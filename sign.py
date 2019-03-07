from __future__ import unicode_literals
import re, json
import requests
import base64
import time
import hashlib

from copy import deepcopy
from collections import OrderedDict

from django.db import models
from django.urls import reverse, reverse_lazy
from django.core.cache import cache
from django.contrib.contenttypes.models import ContentType
from django.utils.safestring import mark_safe
from django.utils.html import escape
from django.conf import settings
from django.template.defaultfilters import linebreaksbr
from django.core.files.base import ContentFile
from django.utils import timezone

from guardian.shortcuts import assign_perm, remove_perm, get_perms
from wand.image import Image

import reversion
from api_sync_info.models import update_api_sync_info
from api_sync_info.mixins import ApiSyncInfoMixin
from sign_attribute.models import ModelWithAttributes, Attribute
from sign_attribute.utils import font_color
from sign_attribute.cache import thread_local_cache
from comment.models import ConversationMixin
from state.models import State
from sign_message.utils import get_dimensions_of_svg
from color.models import Color
from remote_job.signals import jobber

@reversion.register
class Position(ConversationMixin, ModelWithAttributes):
    """ This is a record recording the position on a zone.
        One or more signs will be located at this position
        We'll probably automatically create these position records. They end-user may never know they exist.
    """
    zone = models.ForeignKey("zone.Zone", related_name="positions", on_delete=models.PROTECT, null=True, blank=True)

    # GIS position info (for setting up the map)
    # This will probably be replaced by more detailed information about the image position/rotation.
    lat = models.DecimalField(max_digits=20, decimal_places=17)
    lng = models.DecimalField(max_digits=20, decimal_places=17)
    is_visible = models.BooleanField(default=True)

    # shortcut fks. These are set automatically.
    project = models.ForeignKey("sign_project.Project", related_name="positions", on_delete=models.PROTECT, blank=True, help_text="set automatically")

    class Meta:
        permissions = (
            ('view_position', 'View position'),
        )

    def save(self, *args, **kwargs):
        self.project = self.zone.project
        return super(Position, self).save(*args, **kwargs)

    def attributes(self):
        attributes = self.zone.attributes()
        attributes.update(self.attribute_instances_dict())
        return attributes

    def get_absolute_url(self):
        return reverse_lazy("sign:position_detail", kwargs={'pk':self.id})

    def get_xy(self):
        """ return the x,y position of this sign. (essentially, this is an offset from the top
            left pixel of the zone image.)
        """
        if not hasattr(self, "_xy"):
            try:
                self._xy = self.zone.get_xy_for_latlng(self.lat, self.lng)
            except AttributeError:
                # zone has no blueprint. we default to (-1,-1) here for the sake of the api. (Here's hoping that it doesn't cause any unexpected behaviour anywhere else)
                self._xy = -1,-1
        return self._xy

    def get_x(self):
        return self.get_xy()[0]

    def get_y(self):
        return self.get_xy()[1]

    def set_xy(self, x, y):
        """ set lat and lng based on pixel position """
        self.lat, self.lng = self.zone.get_latlng_for_xy(x,y)

@reversion.register
class Sign(ApiSyncInfoMixin, ConversationMixin, ModelWithAttributes):
    """ The actual sign """
    position = models.ForeignKey(Position, related_name="signs", on_delete=models.CASCADE)
    sign_template = models.ForeignKey("sign_template.SignTemplate", related_name="signs", on_delete=models.PROTECT, null=True, blank=True, verbose_name="Type")
    tags = models.ManyToManyField("tag.Tag", related_name="signs")
    state = models.ForeignKey("state.State", related_name='signs', null=True, on_delete=models.PROTECT)
    facing_direction = models.PositiveSmallIntegerField(default=0, verbose_name="Direction")
    quantity = models.PositiveSmallIntegerField(default=1, blank=True)
    # Number is not unique, and more like a name of the sign. User defined
    number = models.CharField(max_length=255, default="", blank=True)

    created_user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="created_signs", null=True, blank=True, on_delete=models.SET_NULL)
    created_date = models.DateTimeField(default=timezone.now)
    last_modified_date = models.DateTimeField(auto_now=True)

    # shortcut fks. These are set automatically.
    zone = models.ForeignKey("zone.Zone", related_name="signs", null=True,      on_delete=models.PROTECT, blank=True, help_text="set automatically")
    project = models.ForeignKey("sign_project.Project", related_name="signs",   on_delete=models.PROTECT, blank=True, help_text="set automatically")
    phase = models.ForeignKey("phase.Phase", related_name="signs",              on_delete=models.PROTECT, blank=True, help_text="set automatically")
    workflow = models.ForeignKey("workflow.Workflow", related_name="signs",     on_delete=models.PROTECT, blank=True, help_text="set automatically")

    # shortcut search methods
    combined_search_text = models.TextField(default="", blank=True, help_text="set automatically")

    # sort methods
    # default sort is sign_qs.order_by("zone_sort", "number_sort"). All sorts will use that
    # set by global_order_id(), updated on save() or when models are rearranged
    phase_sort = models.CharField(max_length=255, default="", blank=True)
    state_sort = models.CharField(max_length=255, default="", blank=True)
    zone_sort = models.CharField(max_length=255, default="", blank=True)
    sign_template_sort = models.CharField(max_length=255, default="", blank=True)
    # set by zfill(15) of number, updated on save()
    number_sort = models.CharField(max_length=255, default="", blank=True)
    # set by joining tags, update on save()
    tags_sort = models.CharField(max_length=255, default="", blank=True)

    # Conflict Numbers
    has_conflict_type_location_number = models.BooleanField(default=False, help_text="Highlights the sign if type-location-number is duplicated.")
    has_conflict_location_number = models.BooleanField(default=False, help_text="Highlights the sign if location-number is duplicated.")
    has_conflict_type_number = models.BooleanField(default=False, help_text="Highlights the sign if type-number is duplicated.")

    #Override PDF
    override_pdf = models.FileField(max_length=255, null=True, blank=True)
    # automatically created, used to display the pdf on the sign form and expanded list
    override_pdf_as_png = models.FileField(max_length=255, null=True, blank=True)
    override_artwork_up_to_date = models.BooleanField(default=False, help_text="Used to determine if a message should warn the user that artwork is out of date. This turns true when new pdf is uploaded. This turns false when the sign template or attribute instances change.")

    # Json data used in the API. Note, these only update on sign form save, not when inherieted fields change
    message_json = models.TextField(null=True, blank=True)
    repeating_message_json = models.TextField(null=True, blank=True)
    meta_json = models.TextField(null=True, blank=True)

    # Hidden Field to display a green/grey check mark or a red x
    REVIEW_STATE_CHOICES = (('A', 'Approved'), ('R', 'Rejected'),('N', 'Needs Review'))
    review_state = models.CharField(max_length=1, choices=REVIEW_STATE_CHOICES, default='N')

    class Meta:
        permissions = (
            ('view_sign', 'View sign'),
            ('review_sign', 'Review sign'),
        )

    def __init__(self, *args, **kwargs):
        super(Sign, self).__init__(*args, **kwargs)
        self.__original_phase_id = self.phase_id
        self.__original_state_id = self.state_id
        self.__original_zone_id = self.zone_id
        self.__original_sign_template_id = self.sign_template_id
        self.__original_number = self.number
        self.__original_override_pdf = self.override_pdf
        if self.id:
            self.__is_new = False
        else:
            self.__is_new = True

    def save(self, *args, **kwargs):
        self.zone = self.position.zone
        self.project = self.position.project
        if self.state:
            # the only case where state would be null is if we just deleted the state record (which is unlikely)
            self.phase = self.state.phase
            self.workflow = self.state.workflow

        # create the override_pdf_as_png
        # we do the prep work before Super so that if it fails, nothing has changed
        if self.override_pdf and self.__original_override_pdf != self.override_pdf:
            self.override_pdf.seek(0)
            with Image(file=self.override_pdf, resolution=100) as img:
                # remove any transparency
                # img.background_color = Color('white')
                # img.alpha_channel = 'remove'

                # max_length = max(img.width, img.height)
                # if max_length > 1000:
                #     # we need to scale it down a bit.
                #     factor = 1000.0/max_length
                #     new_width = int(img.width * factor)
                #     new_height = int(img.height * factor)
                #     img.resize(width=new_width, height=new_height)

                # Always resize so that width is 500px
                new_width = 500
                new_height = int(img.height * 500 / img.width)
                img.resize(width=new_width, height=new_height)

                with img.convert("PNG") as converted_img:
                    # put the image data in a django friendly object (https://docs.djangoproject.com/en/dev/ref/files/file/#django.core.files.File)
                    override_pdf_as_png_cf = ContentFile(converted_img.make_blob())
                    override_pdf_as_png_name = u"sign_override_artwork_%s.png" % self.id
                    self.override_pdf_as_png.save(override_pdf_as_png_name, override_pdf_as_png_cf, save=False)
                    self.override_artwork_up_to_date = True

        # reset review state if state changes
        if self.state_id and self.__original_state_id != self.state_id:
            self.review_state = 'N'

        # update sorting methods if changed
        if self.id:
            if self.phase_id:
                if self.__original_phase_id != self.phase_id:
                    self.phase_sort = self.phase.global_order_id()
            else:
                self.phase_sort = ""

            if self.state_id:
                if self.__original_state_id != self.state_id:
                    self.state_sort = self.state.global_order_id()
            else:
                self.state_sort = ""

            if self.zone_id:
                if self.__original_zone_id != self.zone_id:
                    self.zone_sort = self.zone.global_order_id()
            else:
                self.zone_sort = ""

            if self.sign_template_id:
                if self.__original_sign_template_id != self.sign_template_id:
                    self.sign_template_sort = self.sign_template.global_order_id()
            else:
                self.sign_template_sort = ""

            if self.number:
                if self.__original_number != self.number:
                    # We want to be able to sort decimals using SQL sort, so we zero fill left and right of the decimal
                    # ['01', '1.11', '1.11.1']
                    # ['000000000000001.000000000000000',
                    #  '000000000000001.110000000000000',
                    #  '000000000000001.11.100000000000']
                    num_split = self.number.split(".")
                    left = num_split[0]
                    if len(num_split) == 1:
                        # No decimal
                        right = ""
                    else:
                        # one or more decimals
                        right = ".".join(num_split[1:])

                    self.number_sort = "{0}.{1}".format(left.zfill(15), right.ljust(15,"0"))
            else:
                self.number_sort = ""

        else:
            # new sign
            if self.phase:
                self.phase_sort = self.phase.global_order_id()
            else:
                self.phase_sort = ""

            if self.state:
                self.state_sort = self.state.global_order_id()
            else:
                self.state_sort = ""

            if self.zone:
                self.zone_sort = self.zone.global_order_id()
            else:
                self.zone_sort = ""

            if self.sign_template:
                self.sign_template_sort = self.sign_template.global_order_id()
            else:
                self.sign_template_sort = ""

            if self.number:
                # We want to be able to sort decimals using SQL sort, so we zero fill left and right of the decimal
                # ['01', '1.11', '1.11.1']
                # ['000000000000001.000000000000000',
                #  '000000000000001.110000000000000',
                #  '000000000000001.11.100000000000']
                num_split = self.number.split(".")
                left = num_split[0]
                if len(num_split) == 1:
                    # No decimal
                    right = ""
                else:
                    # one or more decimals
                    right = ".".join(num_split[1:])

                self.number_sort = "{0}.{1}".format(left.zfill(15), right.ljust(15,"0"))
            else:
                self.number_sort = ""

        if self.__is_new or self.__original_number != self.number or self.__original_sign_template_id != self.sign_template_id or self.__original_zone_id != self.zone_id:
            # check for id conflicts.
            if self.sign_template and self.zone:
                # We need to remove the highlighting when there is no longer a conflict
                # Start by removing the highlight
                self.has_conflict_type_location_number = False
                # We exclude ourself because it cause problems when multiple saves occur
                qs = Sign.objects.filter(project=self.project, sign_template_id=self.__original_sign_template_id, zone_id=self.__original_zone_id, number=self.__original_number).exclude(id=self.id)
                if len(qs) == 1:
                    # There is only one other conflict so remove the highlighting from the other
                    for sign in qs:
                        sign.has_conflict_type_location_number = False
                        sign.save()

                qs = Sign.objects.filter(project=self.project, sign_template=self.sign_template, zone=self.zone, number=self.number).exclude(id=self.id)
                if len(qs) > 0:
                    # If there is more than 0 signs, there is a conflict
                    for sign in qs:
                        if not sign.has_conflict_type_location_number:
                            sign.has_conflict_type_location_number = True
                            sign.save()

                    self.has_conflict_type_location_number = True

            if self.zone:
                # We need to remove the highlighting when there is no longer a conflict
                # Start by removing the highlight
                self.has_conflict_location_number = False
                # We exclude ourself because it cause problems when multiple saves occur
                qs = Sign.objects.filter(project=self.project, zone_id=self.__original_zone_id, number=self.__original_number).exclude(id=self.id)
                if len(qs) == 1:
                    # There is only one other conflict so remove the highlighting from the other
                    for sign in qs:
                        sign.has_conflict_location_number = False
                        sign.save()

                qs = Sign.objects.filter(project=self.project, zone=self.zone, number=self.number).exclude(id=self.id)
                if len(qs) > 0:
                    # If there is more than 0 signs, there is a conflict
                    for sign in qs:
                        if not sign.has_conflict_location_number:
                            sign.has_conflict_location_number = True
                            sign.save()

                    self.has_conflict_location_number = True

            if self.sign_template:
                # We need to remove the highlighting when there is no longer a conflict
                # Start by removing the highlight
                self.has_conflict_type_number = False
                # We exclude ourself because it cause problems when multiple saves occur
                qs = Sign.objects.filter(project=self.project, sign_template_id=self.__original_sign_template_id, number=self.__original_number).exclude(id=self.id)
                if len(qs) == 1:
                    # There is only one other conflict so remove the highlighting from the other
                    for sign in qs:
                        sign.has_conflict_type_number = False
                        sign.save()

                qs = Sign.objects.filter(project=self.project, sign_template=self.sign_template, number=self.number).exclude(id=self.id)
                if len(qs) > 0:
                    # If there is more than 0 signs, there is a conflict
                    for sign in qs:
                        if not sign.has_conflict_type_number:
                            sign.has_conflict_type_number = True
                            sign.save()

                    self.has_conflict_type_number = True


        # tags are handled through the m2m_changed signal handler below

        # update phase mandates t0 include zone/type
        if self.phase:
            if self.zone and (not self.id or self.__original_zone_id != self.zone_id) and self.zone not in self.phase.zones.all():
                self.phase.zones.add(self.zone)
            if self.sign_template and (not self.id or self.__original_sign_template_id != self.sign_template_id) and self.sign_template not in self.phase.sign_templates.all():
                self.phase.sign_templates.add(self.sign_template)

        result = super(Sign, self).save(*args, **kwargs)

        # invalidate cached sign label (just in case sign_template has changed)
        # todo: check whether sign_template has actually changed
        keys = [
            "sign_unicode:%s" % self.id,
            "sign_svg_as_png:%s" % self.id,
            "sign_message_html:%s" % self.id,
        ]
        cache.delete_many(keys)
        job = jobber.send(name='sign:generate_artwork', sign_ids=[self.id,], delay_seconds=30)

        if self.state and (self.__is_new or self.__original_state_id != self.state_id):
            old_state = State.objects.filter(pk=self.__original_state_id)
            if old_state:
                self.assign_remove_perms(old_state[0])
            else:
                self.assign_remove_perms(None)

        return result

    def clone(self, request):
        """Used to copy a sign and change it's id including position, fields and attributes"""
        # Declare a revision block.
        with reversion.create_revision():
            new_sign = deepcopy(self)
            new_sign.number = "{0} (cloned)".format(new_sign.number)
            new_sign.id = None
            # create a new position
            position = deepcopy(self.position)
            position.id = None
            position.save()
            new_sign.position = position
            new_sign.save()

            # special handling for tags, needs an ID before many-to-many relationship can be used.
            new_sign.tags = self.tags.all()
            new_sign.save()

            # Message and meta
            for old_ai in self.attribute_instances.all():
                new_ai = deepcopy(old_ai)
                new_ai.id = None
                new_ai.content_object = new_sign
                # ai.object_id = sign.id
                new_ai.save()

            # Repeating
            for old_message in self.sign_messages.all():
                new_message = deepcopy(old_message)
                new_message.id = None
                new_message.sign = new_sign
                new_message.save()

                for old_ai in old_message.attribute_instances.all():
                    new_ai = deepcopy(old_ai)
                    new_ai.id = None
                    new_ai.content_object = new_message
                    new_ai.save()

            reversion.set_user(request.user)
            reversion.set_comment(json.dumps({
                'title': str(new_sign),       # 'B1-004', '7 signs'
                'url': str(new_sign.get_absolute_url()),
                'summary': "created this sign based on <a href='{0}'>{0}</a>".format(self.get_absolute_url()),
                }, indent=4))

            if new_sign.state:
                new_sign.assign_remove_perms(None)

        return new_sign

    def clone_with_attachments(self, request):
        """Used to copy a sign and change it's id including position, fields and attributes"""
        # Declare a revision block.
        with reversion.create_revision():
            new_sign = deepcopy(self)
            new_sign.number = "{0} (cloned)".format(new_sign.number)
            new_sign.id = None
            # create a new position
            position = deepcopy(self.position)
            position.id = None
            position.save()
            new_sign.position = position
            new_sign.save()

            # special handling for tags, needs an ID before many-to-many relationship can be used.
            new_sign.tags = self.tags.all()
            new_sign.save()

            # Message and meta
            for old_ai in self.attribute_instances.all():
                new_ai = deepcopy(old_ai)
                new_ai.id = None
                new_ai.content_object = new_sign
                # ai.object_id = sign.id
                new_ai.save()

            # Repeating
            for old_message in self.sign_messages.all():
                new_message = deepcopy(old_message)
                new_message.id = None
                new_message.sign = new_sign
                new_message.save()

                for old_ai in old_message.attribute_instances.all():
                    new_ai = deepcopy(old_ai)
                    new_ai.id = None
                    new_ai.content_object = new_message
                    new_ai.save()

            reversion.set_user(request.user)
            reversion.set_comment(json.dumps({
                'title': str(new_sign),       # 'B1-004', '7 signs'
                'url': str(new_sign.get_absolute_url()),
                'summary': "created this sign based on <a href='{0}'>{0}</a>".format(self.get_absolute_url()),
                }, indent=4))

            if new_sign.state:
                new_sign.assign_remove_perms(None)

            # Copy attachments only
            new_conversation = new_sign.conversation()
            for old_comment in self.conversation().comments.all().reverse():
                if old_comment.attachment:
                    new_comment = deepcopy(old_comment)
                    new_comment.id = None
                    new_comment.conversation = new_conversation
                    new_comment.save()

        return new_sign

    def update_combined_search_text(self):
        """ You must save after this method. This does not happen in the save method because attributes are created after """
        combined_search_text = u""

        # message and meta Attributes
        attributes = list(Attribute.objects.filter(
                                is_inheritable=False,
                                sign_template_attributes__is_repeating=False,
                                sign_template_attributes__sign_template=self.sign_template
                                ).distinct().order_by('sign_template_attributes'))
        for a in attributes:
            value, source = self.local_attributes().get(a.slug, ('',None))
            combined_search_text += u" " + unicode(value)


        # repeating attributes
        attributes_repeating = list(Attribute.objects.filter(
                                is_inheritable=False,
                                group="message",
                                sign_template_attributes__is_repeating=True,
                                sign_template_attributes__sign_template=self.sign_template
                                ).distinct().order_by('sign_template_attributes'))
        message_list = list(self.sign_messages.all())
        number_of_real_messages = min(len(message_list), getattr(self.sign_template, 'number_of_messages', 0))
        # (we take the min because there may be message objects created and # of messages reduced)

        for k in range(number_of_real_messages):
            message = message_list[k]
            message_dict = message.attribute_instances_dict()

            for a in attributes_repeating:
                try:
                    value, source = message_dict.get(a.slug,('',None))
                except KeyError:
                    value = ""
                combined_search_text += u" " + unicode(value)

        self.combined_search_text = combined_search_text

    def should_highlight_type(self):
        # Used on sign form to add highlighting of duplicates
        return self.project.highlight_duplication == "0" and ((self.project.auto_numbering == "2" and self.has_conflict_type_location_number) or (self.project.auto_numbering == "3" and self.has_conflict_type_number))

    def should_highlight_location(self):
        # Used on sign form to add highlighting of duplicates
        return self.project.highlight_duplication == "0" and ((self.project.auto_numbering == "2" and self.has_conflict_type_location_number) or (self.project.auto_numbering == "1" and self.has_conflict_location_number))

    def should_highlight_number(self):
        # Used on sign form to add highlighting of duplicates
        return self.should_highlight_type() or self.should_highlight_location()

    def assign_remove_perms(self, old_state):
        """When ever a sign is saved on the form or by state action"""
        position = self.position
        new_state = self.state
        if old_state:
            old_phase_member_group = old_state.phase.get_member_group()
            old_phase_viewer_group = old_state.phase.get_viewer_group()
            old_state_viewer_group = old_state.get_viewer_group()
            remove_perm('change_sign', old_phase_member_group, self)
            remove_perm('view_sign', old_phase_member_group, self)
            remove_perm('view_sign', old_phase_viewer_group, self)
            remove_perm('view_sign', old_state_viewer_group, self)
            remove_perm('review_sign', old_state_viewer_group, self)

            # can we remove perms to the position? Check all other signs under this position
            other_sign_phase_member_change = False
            other_sign_phase_member_view = False
            other_sign_phase_viewer = False
            other_sign_state_viewer = False
            for other_sign in position.signs.exclude(pk=self.id):
                #if we have permission to another sign under this position, flag True
                if 'change_sign' in get_perms(old_phase_member_group, other_sign):
                    other_sign_phase_member_change = True

                if 'view_sign' in get_perms(old_phase_member_group, other_sign):
                    other_sign_phase_member_view = True

                if 'view_sign' in get_perms(old_phase_viewer_group, other_sign):
                    other_sign_phase_viewer = True

                if 'view_sign' in get_perms(old_state_viewer_group, other_sign):
                    other_sign_state_viewer = True

            # if no other sign on this position has perms, remove the position perms
            if not other_sign_phase_member_change:
                remove_perm('change_position', old_phase_member_group, position)
            if not other_sign_phase_member_view:
                remove_perm('view_position', old_phase_member_group, position)
            if not other_sign_phase_viewer:
                remove_perm('view_position', old_phase_viewer_group, position)
            if not other_sign_state_viewer:
                remove_perm('view_position', old_state_viewer_group, position)

        new_phase_member_group = new_state.phase.get_member_group()
        new_phase_viewer_group = new_state.phase.get_viewer_group()
        new_state_viewer_group = new_state.get_viewer_group()
        assign_perm('change_sign', new_phase_member_group, self)
        assign_perm('view_sign', new_phase_member_group, self)
        assign_perm('view_sign', new_phase_viewer_group, self)
        assign_perm('view_sign', new_state_viewer_group, self)
        assign_perm('review_sign', new_state_viewer_group, self)

        assign_perm('change_position', new_phase_member_group, position)
        assign_perm('view_position', new_phase_member_group, position)
        assign_perm('view_position', new_phase_viewer_group, position)
        assign_perm('view_position', new_state_viewer_group, position)

    def attributes(self):
        attributes = {}

        # sign template
        if self.sign_template:
            attributes.update(self.sign_template.attributes())

        # Cleaner, slower way:
        # attributes.update(self.position.attributes())

        # Longer, faster way:
        # zone
        cache_key = "zone:%s" % self.zone_id
        result = thread_local_cache.get(cache_key)
        if result is not None:
            attributes.update(result)
        else:
            attributes.update(self.zone.attributes())

        # position
        cache_key = u"sign.Position:{0}:attribute_instances_dict".format(self.position_id)
        result = cache.get(cache_key)
        if result is not None:
            attributes.update(result)
        else:
            attributes.update(self.position.attribute_instances_dict())

        # local
        attributes.update(self.attribute_instances_dict())
        ct = ContentType.objects.get_for_model(self)
        if self.sign_template:
            attributes['sign_template'] = (unicode(self.sign_template), {'content_type': ct.id, 'object_id': self.id})
        attributes['number'] = (self.number, {'content_type': ct.id, 'object_id': self.id})
        attributes['sign_id'] = ('{0} - {1} - {2}'.format(attributes['type.short_code_combo'][0], attributes['location.short_code_combo'][0], self.number), {'content_type': ct.id, 'object_id': self.id})
        attributes['last_modified_date'] = (self.last_modified_date.now().strftime("%Y-%m-%d"), {'content_type': ct.id, 'object_id': self.id})
        attributes['last_modified_year'] = (self.last_modified_date.now().strftime("%Y"), {'content_type': ct.id, 'object_id': self.id})
        attributes['last_modified_month'] = (self.last_modified_date.now().strftime("%m"), {'content_type': ct.id, 'object_id': self.id})
        attributes['last_modified_day'] = (self.last_modified_date.now().strftime("%d"), {'content_type': ct.id, 'object_id': self.id})

        return attributes

    def local_attributes(self):
        """ like attributes, but without any inheritance """
        attributes = {}

        # local
        attributes.update(self.attribute_instances_dict())
        ct = ContentType.objects.get_for_model(self)
        if self.sign_template:
            attributes['sign_template'] = (unicode(self.sign_template), {'content_type': ct.id, 'object_id': self.id})
        attributes['number'] = (self.number, {'content_type': ct.id, 'object_id': self.id})

        return attributes

    def repeating_attributes(self):
        """Used on the sign form for templating fields"""
        attributes = {}
        repeating_attributes = []
        ct = ContentType.objects.get_for_model(self)
        for sta in self.sign_template.sign_template_attributes.filter(is_repeating=True):
            repeating_attributes.append(sta.attribute)
        for i, m in enumerate(self.sign_messages.all()):
            prefixes = ["message_%s" % (i+1)]
            if i == 0:
                prefixes.append("message")
            aid = m.attribute_instances_dict()
            for a in repeating_attributes:
                v = aid.get(a.slug, ('', None))[0]
                # value = a.prep_for_svg(value)
                for prefix in prefixes:
                    field_key = "{0}.{1}".format(prefix, a.slug)
                    attributes[field_key] = (v, {'content_type': ct.id, 'object_id': self.id})
        return attributes

    def get_message_json(self):
        """ Create a json of message attributes. Used in API """
        message_dict = OrderedDict()
        if self.sign_template:
            attributes_message = self.sign_template.message_attributes()
            for a in attributes_message:
                value, source = self.attributes().get(a.slug, ('',None))
                if value != '-unknown-':
                    value = escape(value)
                    message_dict[unicode(a)] = {'type': a.field_type, 'value': value}
        if message_dict:
            return json.dumps(message_dict, indent=4)
        return None

    def get_repeating_message_json(self):
        """ Create a json of repeating message attributes. Used in API """
        repeating_message_dict = OrderedDict()
        if self.sign_template:
            attributes_repeating = self.sign_template.repeating_attributes()

            for a in attributes_repeating:
                repeating_message_dict[unicode(a)] = {'type': a.field_type, 'values': []}

            if attributes_repeating:
                message_list = list(self.sign_messages.all())
                number_of_real_messages = min(len(message_list), self.sign_template.number_of_repeating())
                # we take the min because there may be message objects created and # of messages reduced
                for k in range(number_of_real_messages):
                    message = message_list[k]
                    for a in attributes_repeating:
                        value, source = message.attribute_instances_dict().get(a.slug, ("", None))
                        repeating_message_dict[unicode(a)]['values'].append(value)
                for k in range(number_of_real_messages, self.sign_template.number_of_repeating()):
                    repeating_message_dict[unicode(a)]['values'].append("")
        if repeating_message_dict:
            return json.dumps(repeating_message_dict, indent=4)
        return None

    def get_meta_json(self):
        """ Create a json of message attributes. Used in API """
        meta_dict = OrderedDict()
        if self.sign_template:
            attributes_meta = self.sign_template.meta_attributes()
            aid = self.attribute_instances_dict()
            for a in attributes_meta:
                value, source = aid.get(a.slug, ('',None))
                if value != '-unknown-':
                    value = escape(value)
                    meta_dict[unicode(a)] = {'type': a.field_type, 'value': value}
        if meta_dict:
            return json.dumps(meta_dict, indent=4)
        return None

    def message_html(self):
        """ Used to show a brief summary of message info for sign hover and expanded list view """
        if self.id:
            key = "sign_message_html:%s" % self.id
            result = cache.get(key)
            if result:
                return result

        html = ""
        if self.sign_template:
            attributes_message = self.sign_template.message_attributes()
            sign_attribute_data = self.attributes()
            attribute_instances_text_dict = self.attribute_instances_text_dict()
            for a in attributes_message:
                value, source = sign_attribute_data.get(a.slug, ('',None))
                if value and value != '-unknown-':
                    value = escape(value)
                    html += "<b>"+unicode(a)+"</b>"
                    if a.field_type == "color":
                        html += """<style>
                                    @media all{
                                    .color_"""+value+"""{
                                        background-color: #"""+value+""" !important;
                                        color: #"""+font_color(value)+""" !important;
                                        padding: 3px !important;
                                        }
                                    }
                                </style>"""
                        html += "<p class='color_"+value+"'>"+value+"</p>"
                    elif a.field_type in ["color_x",'color_t']:
                        color = Color.objects.get(id=value)
                        html += """<style>
                                    @media all{
                                    .color_"""+color.color+"""{
                                        background-color: #"""+color.color+""" !important;
                                        color: #"""+font_color(color.color)+""" !important;
                                        padding: 3px !important;
                                        }
                                    }
                                </style>"""
                        html += "<p class='color_"+color.color+"'>"+color.name+"</p>"
                    elif a.field_type in ["icon",'icon_t']:
                        unused_value, source, text_value = attribute_instances_text_dict.get(a.slug, ('',None,''))
                        html += "<p><img height='15px' src='/sign_message/icon/"+value+"/thumbnail_url/40/' alt='"+text_value+"'></p>"
                    else:
                        html += "<p>"+linebreaksbr(value)+"</p>"

            attributes_repeating = self.sign_template.repeating_attributes()

            message_list = list(self.sign_messages.all())
            number_of_real_messages = min(len(message_list), self.sign_template.number_of_repeating())
            # we take the min because there may be message objects created and # of messages reduced
            # Find the first non empty value and set the counter to remove empty values
            if attributes_repeating:
                attributes_repeating_count = len(attributes_repeating)
                for k in range(number_of_real_messages-1,-1,-1):
                    #check the last element to see if its empty
                    message = message_list[k]
                    message_data = message.attribute_instances_dict()
                    for a in attributes_repeating:
                        value, source = message_data.get(a.slug, (None, None))
                        if value:
                            break
                    if value:
                        break
                    else:
                        number_of_real_messages -= 1

                if number_of_real_messages != 0:
                    html += "<table class='attributes_repeating_table'><thead><tr>"
                    for a in attributes_repeating:
                        html += "<th>"+unicode(a)+"</th>"
                    html += "</tr></thead><tbody>"

                    side_dict = self.sign_template.side_dict()
                    column_dict = self.sign_template.column_dict()
                    for k in range(number_of_real_messages):
                        side_num = side_dict.get(k, None)
                        column_num = column_dict.get(k, None)
                        if side_num:
                            html += '<tr class="tr_side_number_expanded_list">'
                            html += '<td colspan="{0}"><b>Side {1}</b></td>'.format(attributes_repeating_count, side_num)
                            html += '</tr>'
                        if column_num:
                            html += '<tr class="tr_column_number_expanded_list">'
                            html += '<td colspan="{0}">Column {1}</td>'.format(attributes_repeating_count, column_num)
                            html += '</tr>'

                        html += "<tr>"
                        message = message_list[k]
                        # TODO: if performance is an issue, use the following
                        # message_dict = message.attribute_instances_dict()
                        message_dict = {}
                        attribute_instances_text_dict = message.attribute_instances_text_dict()
                        for ai in message.attribute_instances.all():
                            message_dict[ai.attribute.id] = ai.value()

                        for a in attributes_repeating:
                            try:
                                value = message_dict[a.id]
                            except KeyError:
                                value = ""
                            if not value:
                                value = ""
                            if a.field_type == "color":
                                value = escape(value)
                                html += """<style>
                                            @media all{
                                            td.color_"""+value+"""{
                                                background-color: #"""+value+""" !important;
                                                color: #"""+font_color(value)+""" !important;
                                                padding: 3px !important;
                                                }
                                            }
                                        </style>"""
                                html += "<td class='color_"+value+"'>"+value+"</td>"
                            elif a.field_type in ["color_x",'color_t']:
                                # color = Color.objects.get(id=value)
                                if value:
                                    html += """<style>
                                                @media all{
                                                td.color_"""+value.color+"""{
                                                    background-color: #"""+value.color+""" !important;
                                                    color: #"""+font_color(value.color)+""" !important;
                                                    padding: 3px !important;
                                                    }
                                                }
                                            </style>"""
                                    html += "<td class='color_"+value.color+"'>"+value.name+"</td>"
                                else:
                                    html += "<td></td>"
                            elif a.field_type in ["icon",'icon_t']:
                                if value:
                                    unused_value, source, text_value = attribute_instances_text_dict.get(a.slug, ('',None, ''))
                                    html += "<td style='text-align:center'><img height='15px' src='/sign_message/icon/"+str(value.id)+"/thumbnail_url/40/' alt='"+text_value+"'></td>"
                                else:
                                    html += "<td></td>"
                            else:
                                if not value:
                                    value = "\n"
                                html += "<td>"+linebreaksbr(escape(value))+"</td>"
                        html += "</tr>"
                    html += "</tbody></table>"

        if self.id:
            cache.set(key, html, None)      # invalidation happens at the sign_template, and attribute level
        return html

    def meta_html(self):
        """ Used to show a brief summary of message info for sign hover and expanded list view """
        html = ""
        if self.sign_template:
            attributes_meta = self.sign_template.meta_attributes()
            aid = self.attribute_instances_dict()
            aid_text = self.attribute_instances_text_dict()
            for a in attributes_meta:
                value = aid.get(a.slug, ('',None))[0]
                if value and value != '-unknown-':
                    value = escape(value)
                    html += "<b>"+unicode(a)+"</b>"
                    if a.field_type == "color":
                        html += """<style>
                                    @media all{
                                    .color_"""+value+"""{
                                        background-color: #"""+value+""" !important;
                                        color: #"""+font_color(value)+""" !important;
                                        padding: 3px !important;
                                        }
                                    }
                                </style>"""
                        html += "<p class='color_"+value+"'>"+value+"</p>"
                    elif a.field_type in ["color_x",'color_t']:
                        color = Color.objects.get(id=value)
                        html += """<style>
                                    @media all{
                                    .color_"""+color.color+"""{
                                        background-color: #"""+color.color+""" !important;
                                        color: #"""+font_color(color.color)+""" !important;
                                        padding: 3px !important;
                                        }
                                    }
                                </style>"""
                        html += "<p class='color_"+color.color+"'>"+color.name+"</p>"
                    elif a.field_type in ["icon",'icon_t']:
                        unused_value, source, text_value = aid_text.get(a.slug, ('',None,''))
                        html += "<p><img height='15px' src='/sign_message/icon/"+value+"/thumbnail_url/40/' alt='"+text_value+"'></p>"
                    else:
                        html += "<p>"+linebreaksbr(value)+"</p>"
        return html

    def message_html_for_api(self):
        """ Sebastian wants None instead of empty strings """
        html = self.message_html()
        if html:
            return html
        return None

    def meta_html_for_api(self):
        """ Sebastian wants None instead of empty strings """
        html = self.meta_html()
        if html:
            return html
        return None

    def attributes_prepped_for_svg(self):
        """ The attributes method returns simple string values, this method returns svg-ready values
            This is useful for special attribute types, like icons.

            This is used in the method svg_code
        """
        d = self.attributes()
        for k,v in d.items():
            d[k] = v[0]

        if self.sign_template:
            # for a in self.sign_template.required_attributes.all():
            for sta in self.sign_template.sign_template_attributes.filter(is_repeating=False, attribute__group='message'):
                a = sta.attribute
                d[a.slug] = a.prep_for_svg(d[a.slug])
        return d

    def tag_list(self):
        """ returns a list of the tags, in plain text """
        if self.id:
            return self.tags.all().values_list('tag', flat=True)
        else:
            return []

    def __unicode__(self):
        """ this will be generated based on:
            the template's definition
            local attributes
        """
        if self.id:
            key = "sign_unicode:%s" % self.id
            result = cache.get(key)
            if result:
                # decode allows for ascii characters like bullets
                return result.decode('utf-8')

        if not self.sign_template or not self.zone:
            return "New Sign"

        # kwargs = {}
        # NOTE: We can only cache this valyue because this label now depends on sign_template, and LOCAL attributes only. If we open it up to include interited attributes, then we need to disable, or adjust our invalidation logic for this cache.
        # for k,v in self.local_attributes().items():
        #     kwargs[k] = v[0]

        # render it
        # result = u"{sign_template}".format(**kwargs)     # default value
        # template = self.sign_template.name_template_str
        # while template:
        #     try:
        #         result = template.format(**kwargs)
        #         break
        #     except KeyError as e:
        #         # the template specifies something that doesn't exist within kwargs. Probably caused by a typo.
        #         # Let's just remove this invalid argument from the template string.
        #         template = template.replace(u"{%s}" % e.message, "")

        kwargs = {'type': self.sign_template.full_short_code(), 'location': self.zone.full_short_code(), 'number': self.number}
        template = self.project.sign_id
        result = template.format(**kwargs)

        if self.id:
            cache.set(key, result, None)      # invalidation happens at the sign_template, and attribute level
        return result

    def get_absolute_url(self):
        # return reverse_lazy("sign:detail", kwargs={'pk':self.id})
        return reverse_lazy("sign:update", kwargs={'pk':self.id})

    def get_svg_url(self, generate=True):
        """ used in the form, where we want absolutely zero caching """
        if not hasattr(self, '_get_svg_url'):
            # url = reverse_lazy("sign:svg", kwargs={'pk':self.id}) + "?t={0}".format(time.time())
            url = reverse_lazy("sign:svg_as_png", kwargs={'pk':self.id}) + "?t={0}&generate={1}".format(time.time(), generate)
            self._get_svg_url = url
        return self._get_svg_url

    def get_pdf_url(self):
        """ used in the form, where we want absolutely zero caching """
        if not hasattr(self, '_get_pdf_url'):
            # url = reverse_lazy("sign:svg", kwargs={'pk':self.id}) + "?t={0}".format(time.time())
            url = reverse_lazy("sign:pdf_as_png", kwargs={'pk':self.id}) + "?t={0}".format(time.time())
            self._get_pdf_url = url
        return self._get_pdf_url

    def get_artwork_url(self):
        """ used in the api to fetch either the svg_as_png or the pdf_as_png """
        if self.id and (self.override_pdf or (self.sign_template and self.sign_template.svg_code)):
            url = reverse_lazy("sign:artwork", kwargs={'pk':self.id}) + "?t={0}".format(time.time())
            return url
        return None

    def position_index_number(self):
        """ return the index of this sign in the list of signs for this position """
        return list(self.position.signs.all().values_list('id', flat=True)).index(self.id) + 1

    @staticmethod
    def myreplace(haystack, needle, replacement):
        if replacement is None:
            replacement = ''
        new = haystack.replace(u"{%s}" % needle, replacement)
        new = new.replace(u"{ %s }" % needle, replacement)
        return new

    def svg_context(self, text_to_vector=False):
        """ create a context dictionary for Bill's svg rendering code. """
        attribute_dict = self.attributes()
        context = {
            'number': self.number,
            'type.short_code_combo': attribute_dict.get('type.short_code_combo'),
            'location.short_code_combo': attribute_dict.get('location.short_code_combo'),
            'sign_id': attribute_dict.get('sign_id'),
            'last_modified_date': attribute_dict.get('last_modified_date'),
            'last_modified_year': attribute_dict.get('last_modified_year'),
            'last_modified_month': attribute_dict.get('last_modified_month'),
            'last_modified_day': attribute_dict.get('last_modified_day'),
            'svg_options': {
                'text_to_vector': text_to_vector,
                'embed_svg': True,
                'fonts_list': self.sign_template.fonts_list(),
            },
        }
        if self.sign_template and self.sign_template.svg_code:
            for sta in self.sign_template.sign_template_attributes.filter(is_repeating=False, attribute__group='message'):
                a = sta.attribute
                v = attribute_dict.get(a.slug, ('', None))[0]
                dv = a.prep_for_dynamic_svg(v)
                if dv:
                    context[a.slug] = dv

            repeating_attributes = []
            for sta in self.sign_template.sign_template_attributes.filter(is_repeating=True):
                repeating_attributes.append(sta.attribute)

            rows = []
            for i, m in enumerate(self.sign_messages.all()):
                aid = m.attribute_instances_dict()
                row = {}
                for a in repeating_attributes:
                    v = aid.get(a.slug, ('', None))[0]
                    dv = a.prep_for_dynamic_svg(v)
                    if dv:
                        row[a.slug] = dv
                rows.append(row)
            # context['repeat'] = rows

            # split into sides and columns (based on the sign's number of sides)
            # Example:
            # context = {
            #     'side_1':{
            #         'column_1': {
            #             'repeat': [...]
            #         }
            #         'column_2': {
            #             'repeat': [...]
            #         }
            #     }
            #     'side_2':{
            #         'column_1': {
            #             'repeat': [...]
            #         }
            #         'column_2': {
            #             'repeat': [...]
            #         }
            #     }
            # }

            num_sides = int(self.sign_template.number_of_sides)
            num_cols = self.sign_template.number_of_columns
            num_messages = self.sign_template.number_of_messages
            for i in range(num_sides):
                side_num = i + 1     # start counting at 1, rather than 0, because there are humans involved.
                side_key = "side_{0}".format(side_num)
                if side_key not in context:
                    # context[side_key] = {'column_1':{'repeat':[]}}
                    context[side_key] = {}

                for j in range(num_cols):
                    col_num = j + 1     # start counting at 1, rather than 0, because there are humans involved.
                    col_key = "column_{0}".format(col_num)
                    if col_key not in context[side_key]:
                        context[side_key][col_key] = {'repeat':[]}

                    for k in range(num_messages):
                        row_num = (i*num_cols*num_messages) + (j*num_messages) + k
                        try:
                            row = rows[row_num]
                        except IndexError:
                            pass
                        else:
                            context[side_key][col_key]['repeat'].append(row)

        # print context
        return context

    def svg_node_payload(self, text_to_vector=False):
        return {
            'json_data': "base64:" + base64.b64encode(json.dumps(self.svg_context(text_to_vector=text_to_vector), indent=4)),
            'svg_template': "base64:" + base64.b64encode(self.sign_template.svg_code_w_fonts()),
            }

    def svg_code_text_to_vector(self):
        return self.svg_code(text_to_vector=True)

    def svg_code(self, text_to_vector=False):
        if hasattr(self, "_svg_code"):
            return self._svg_code

        if self.sign_template and self.sign_template.svg_code:
            template = self.sign_template.svg_code_w_fonts()

            # determine which 'spec' we are using. Bill's comprehensive `<g id='level'>`, or Aaron's simplified `{level}`?

            use_expander = False
            # look for a tag with `id='repeat'`
            p = re.compile(r"<g .*?id\w*?=\w*?('|\")repeat('|\").*?>")
            if p.search(template):
                use_expander = True
            else:
                for k in self.attributes().keys():
                    # look for g tags like `id='level'`
                    p = re.compile(r"<g .*?id\w*?=\w*?('|\")%s('|\").*?>" % k)
                    if p.search(template):
                        use_expander = True
                        break

            if use_expander:
                # it's a dynamic template

                # make a call to localhost:8081/expand/, with data json_data and svg_template
                payload = self.svg_node_payload(text_to_vector=text_to_vector)
                # ...unless of course we've done this exact same work before.
                # note: is this worth caching? We wrap this function in a conversion to png almost all the time, so this cacheing really just helps when a user is reverting their changes back to a prevoious state, or they have several signs with identical content.

                # removed caching because it doesnt get invalidated when Node updates
                # payload_hash = hashlib.md5(json.dumps(payload)).hexdigest()
                # result = cache.get(payload_hash)
                result = False
                if result:
                    template = result
                else:
                    try:
                        url = "{0}/expand/".format(settings.NODE_DOMAIN)
                        with requests.post(url, data=payload, headers=settings.NODE_HEADERS) as r:
                            r_text = r.text
                    except:
                        # fail gracefully, at least for now
                        # pass
                        raise
                    else:
                        if len(r_text) > 150:
                            template = r_text
                            # cache.set(payload_hash, template, 604800)

                        else:
                            # when text is very short.. it's probably an empty svg document, which is the result of some sort of error in Bill's expansion code.

                            # it would be nice to know whether this error is because of a flaw in the node logic (developer-error), or whether the svg template is malformed (user-error). Currently, there's no way for us to know.
                            # if it's a developer-error, we'd want to raise an exception here, with additional context to debug and resolve.
                            # if it's a user-error, we'd want to return the failure to the user, probably using a placeholder image with text saying "Artwork generation failed, your template appears to be malformed".
                            # both of these options would be better served by node. That is to say: Node should be raising an error and logging to sentry itself, or returning a placeholder image in the case of a malformed template. It would then be djano's responsibility (here) to raise an exception when node returns an unexpected result for any other (developer-related) reason.
                            pass
                            # raise Exception("The node expansion appears to have returned an invalid response: '{0}'".format(r_text))
                            # Todo:
                            # create place holder indicating failed artwork


            # This is a fix to funny characters like the "e" with the thingy on top
            # This may have un-intended effect for certain edge case characters
            if type(template) == type(str()):
                template = unicode(template, "utf-8", errors="ignore")
            template = template.encode('ascii', 'xmlcharrefreplace')
            if "{" in template or "}" in template:
                # it's a static/fixed template

                # inject attribute values for attributes
                for k,v in self.attributes_prepped_for_svg().items():
                    template = self.myreplace(template, k, v)

                # inject attribute values for repeating attributes
                repeating_attributes = []
                for sta in self.sign_template.sign_template_attributes.filter(is_repeating=True):
                    repeating_attributes.append(sta.attribute)
                for i, m in enumerate(self.sign_messages.all()):
                    prefixes = ["message_%s" % (i+1)]
                    if i == 0:
                        prefixes.append("message")
                    aid = m.attribute_instances_dict()
                    for a in repeating_attributes:
                        value = aid.get(a.slug, ('', None))[0]
                        value = a.prep_for_svg(value)
                        for prefix in prefixes:
                            field_key = "{0}.{1}".format(prefix, a.slug)
                            template = self.myreplace(template, field_key, value)

            result = mark_safe(template)

        else:
            result = ""

        self._svg_code = result
        return result

    def svg_code_with_fonts_removed(self, text_to_vector=False):
        """ Just return that SVG component from svg_code(). Essentially this is just stripping out the leading font-related style tag """
        from lxml import etree
        from sign_message.utils import get_lxml_object, get_first_svg_in_lxml
        root = get_lxml_object(u"<div>{0}</div>".format(self.svg_code(text_to_vector=text_to_vector)))
        svg = get_first_svg_in_lxml(root)
        return etree.tostring(svg, pretty_print=True)

    def svg_as_png(self, generate=True):
        """ get the node server to render the svg into a png

            We used to pass the svg_code directly to Node, but that ran us into text encoding headaches (specifically: with the url encoding of font urls within the stylesheet)

            So yes, this is a lot of back and forth.
                The end-user asks django for /<id>/svg_as_png/
                which asks node for /convert_png/
                which asks django for /<id>/svg/
                which asks node for /expand/
                which asks node for /_expand/
        """
        if self.id:
            key = "sign_svg_as_png:%s" % self.id
            result = cache.get(key)
            if result:
                return result
            elif generate==False:
                # return an 'in progress' placeholder (and hope that this is being processed somewhere.)
                with open("{0}/sign/static/sign/generating_artwork.png".format(settings.BASE_DIR)) as f:
                    content = f.read()
                return content

        # x,y = get_dimensions_of_svg(self.sign_template.svg_code)
        x,y = get_dimensions_of_svg(self.svg_code())
        payload = {
            'width': x,
            'height': y,
            'svg_url': settings.DOMAIN + reverse("sign:svg", kwargs={'pk':self.id}) + "?direct=1",
            }
        url = "{0}/convert_png/".format(settings.NODE_DOMAIN)
        with requests.post(url, data=payload, headers=settings.NODE_HEADERS) as r:
            r_content = r.content

        if self.id:
            if len(r_content) > 150:
                # we don't want to cache an error.
                # be mindful that this logic has consequences. If the error is expensive, then not caching it will add to the work on our server.
                cache.set(key, r_content, None)      # invalidation happens at the sign_template, and attribute level
        return r_content

    def svg_debug_code(self):
        payload = self.svg_node_payload()
        s1 = "<form action='http://localhost:8081/expand/' method='post'><input type='hidden' name='json_data' value='{json_data}'><input type='hidden' name='svg_template' value='{svg_template}'><input type='submit' value='render via node (puppeteer)'></form>".format(**payload)
        s2 = "<form action='http://localhost:8081/_expand/' method='post'><input type='hidden' name='json_data' value='{json_data}'><input type='hidden' name='svg_template' value='{svg_template}'><input type='submit' value='render in the browser'></form>".format(**payload)
        return s1 + s2

    def snapshot(self):
        """ take a json snapshot of the sign, including tags, messaging, and local attributes """
        if self.override_pdf:
            custom_artwork = "<a href='{0}'>{1}</a>".format(self.override_pdf.url, unicode(self.override_pdf).rsplit("/",1)[1])
        else:
            custom_artwork = ""

        data = {
            'sign_template': unicode(self.sign_template),
            'state': unicode(self.state),
            'facing_direction': unicode(self.facing_direction),
            'number': unicode(self.number),
            'quantity': unicode(self.quantity),
            'custom_artwork': custom_artwork,
        }

        if self.id:
            data['tags'] = ", ".join([unicode(t) for t in self.tags.all()])

            # repeating attributes
            attributes_repeating = Attribute.objects.filter(
                                    is_inheritable=False,
                                    group="message",
                                    sign_template_attributes__is_repeating=True,
                                    sign_template_attributes__sign_template=self.sign_template
                                    ).order_by('sign_template_attributes')
            for i, m in enumerate(self.sign_messages.all()):
                message_dict = {}
                for k, v in m.attribute_instances_text_dict().items():
                    message_dict[k] = v[2]
                for a in attributes_repeating:
                    try:
                        data['message_{0}.{1}'.format(i + 1,a.slug)] = unicode(message_dict[a.slug])
                    except KeyError:
                        data['message_{0}.{1}'.format(i + 1,a.slug)] = ""

            # regular attributes
            for k, v in self.attribute_instances_dict().items():
                data[k] = unicode(v[0])

        else:
            data['tags'] = ""

        return data

    def auto_set_number(self, **kwargs):
        # set number, based on auto_numbering rules
        if self.project_id:
            project = self.project
        elif 'zone' in kwargs:
            project = kwargs.get('zone').project
        else:
            raise Exception('cannot determine project')
        auto_numbering = project.auto_numbering
        if auto_numbering != "0":
            cd_zone = kwargs.get('zone', self.zone)
            cd_type = kwargs.get('sign_template', self.sign_template)
            if auto_numbering == "1":
                key = "zone_%s:max_sign_number" % cd_zone.id
            elif auto_numbering == "2":
                key = "zone_{0},type_{1}:max_sign_number".format(cd_zone.id, cd_type.id)
            elif auto_numbering == "3":
                key = "type_%s:max_sign_number" % cd_type.id

            largest = cache.get(key)
            if not largest:
                # long form logic to determine this value
                largest = 0
                char_length = 0
                if auto_numbering == "1":
                    max_sign_number_qs = cd_zone.signs.all()
                elif auto_numbering == "2":
                    max_sign_number_qs = cd_zone.signs.filter(sign_template=cd_type)
                elif auto_numbering == "3":
                    max_sign_number_qs = cd_type.signs.all()

                for s in max_sign_number_qs:
                    try:
                        v = int(s.number)
                    except ValueError:
                        v = 0
                    if v > largest:
                        largest = v
                        char_length = len(s.number)
                largest = str(largest).zfill(char_length)
                cache.set(key, largest, None)
            value = str(int(largest)+1).zfill(len(largest))
            self.number = value

models.signals.post_save.connect(update_api_sync_info, sender=Sign)
models.signals.pre_delete.connect(update_api_sync_info, sender=Sign)

def tags_changed(sender, **kwargs):
    # invalidate cached tags_sort
    instance = kwargs.get('instance')
    reverse = kwargs.get('reverse')
    # print "hey!", instance, type(instance), reverse

    if reverse:
        # instance is a tag?
        for sign in instance.signs.all():
            sign.tags_sort = ", ".join(sign.tag_list())
            sign.save()
    else:
        # instance is a sign
        instance.tags_sort = ", ".join(instance.tag_list())
        instance.save()
models.signals.m2m_changed.connect(tags_changed, sender=Sign.tags.through)

def clean_up_position(sender, **kwargs):
    """ Sign post delete clean up position if it has no other signs """
    sign = kwargs.get('instance')
    if not sign.position.signs.all().exclude(pk=sign.id):
        sign.position.delete()
models.signals.post_delete.connect(clean_up_position, sender=Sign)

def generate_artwork(**kwargs):
    """ generate sign artwork
        `job = jobber.send(name='sign:generate_artwork', sign_ids=[self.id,])`
    """
    print "Generating artwork..."
    sign_ids = kwargs.pop('sign_ids')
    st_code_results = {}
    for sign in Sign.objects.filter(id__in=sign_ids):
        # print sign
        # check whether this sign_type has artwork.
        if sign.sign_template_id not in st_code_results:
            st_code_results[sign.sign_template_id] = bool(sign.sign_template.svg_code)

        if st_code_results[sign.sign_template_id]:
            sign.svg_as_png()
    print "Generating artwork. Done."
jobber.connect(generate_artwork, name='sign:generate_artwork', dispatch_uid='sign:generate_artwork')
