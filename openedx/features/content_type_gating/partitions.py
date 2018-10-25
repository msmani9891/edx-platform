import logging
import textwrap

import crum
from django.apps import apps
from django.conf import settings
from django.utils.translation import ugettext_lazy as _

from web_fragments.fragment import Fragment
from lms.djangoapps.commerce.utils import EcommerceService
from lms.djangoapps.courseware.masquerade import (
    get_course_masquerade,
    is_masquerading_as_specific_student,
    get_masquerading_user_group,
)
from xmodule.partitions.partitions import Group, UserPartition, UserPartitionError
from openedx.features.course_duration_limits.config import CONTENT_TYPE_GATING_FLAG

LOG = logging.getLogger(__name__)

CONTENT_GATING_PARTITION_ID = 51
COURSE_MODE_AUDIT = u'audit'


def create_content_gating_partition(course):
    """
    Create and return the Content Gating user partition.
    """

    try:
        content_gate_scheme = UserPartition.get_scheme("content_type_gate")
    except UserPartitionError:
        LOG.warning("No 'content_type_gate' scheme registered, ContentTypeGatingPartitionScheme will not be created.")
        return None

    used_ids = set(p.id for p in course.user_partitions)
    if CONTENT_GATING_PARTITION_ID in used_ids:
        LOG.warning(
            "Can't add 'content_type_gate' partition, as ID {id} is assigned to {partition} in course {course}.".format(
                id=CONTENT_GATING_PARTITION_ID,
                partition=_get_partition_from_id(course.user_partitions, CONTENT_GATING_PARTITION_ID).name,
                course=unicode(course.id)
            )
        )
        return None

    partition = content_gate_scheme.create_user_partition(
        id=CONTENT_GATING_PARTITION_ID,
        name=_(u"Feature-based Enrollments"),
        description=_(u"Partition for segmenting users by access to gated content types"),
        parameters={"course_id": unicode(course.id)}
    )
    return partition


class ContentTypeGatingPartition(UserPartition):
    def access_denied_fragment(self, block, user, user_group, allowed_groups):
        ecomm_service = EcommerceService()
        ecommerce_checkout = ecomm_service.is_enabled(user)
        ecommerce_checkout_link = ''
        ecommerce_bulk_checkout_link = ''
        verified_mode = None
        CourseMode = apps.get_model('course_modes.CourseMode')
        modes = CourseMode.modes_for_course_dict(block.scope_ids.usage_id.course_key)
        verified_mode = modes.get(CourseMode.VERIFIED, '')

        if ecommerce_checkout and verified_mode.sku:
            ecommerce_checkout_link = ecomm_service.get_checkout_page_url(verified_mode.sku)

        if verified_mode is None:
            return None

        request = crum.get_current_request()
        if 'org.edx.mobile' in request.META.get('HTTP_USER_AGENT', ''):
            upsell = ''
        else:
            upsell = textwrap.dedent("""\
                <span class="certDIV_1" style="">
                    <a href="{ecommerce_checkout_link}" class="certA_2">
                        Upgrade to unlock  (${min_price})
                    </a>
                </span>
            """.format(
                ecommerce_checkout_link=ecommerce_checkout_link,
                # TODO: Does this need i18n?
                min_price=verified_mode.min_price,
            ))

        frag = Fragment(textwrap.dedent(u"""\
            <div class=".content-paywall">
                <div>
                    <h3>
                        <span class="fa fa-lock" aria-hidden="true"></span>
                        Verified Track Access
                    </h3>
                    <span style=" padding: 10px 0;">
                        Graded assessments are available to Verified Track learners.
                    </span>
                    {upsell}
                </div>
                <img src="https://courses.edx.org/static/images/edx-verified-mini-cert.png">
            </div>
        """.format(
            upsell=upsell,
        )))
        frag.add_css(textwrap.dedent("""\
            .content-paywall {
                margin-top: 10px;
                border-radius: 5px 5px 5px 5px;
                display: flex;
                justify-content: space-between;
                border: lightgrey 1px solid;
                padding: 15px 20px;
            }

            .content-paywall h3 {
                font-weight: 600;
                margin-bottom: 10px;
            }

            .content-paywall .fa-lock {
                color: black;
                margin-right: 10px;
                font-size: 24px;
                margin-left: 5px;
            }

            .content-paywall .certDIV_1 {
                color: rgb(25, 125, 29);
                height: 20px;
                width: 300px;
                font: normal normal 600 normal 14px / 20px 'Helvetica Neue', Helvetica, Arial, sans-serif;
            }

            .content-paywall .certA_2 {
                text-decoration: underline !important;
                color: rgb(0, 117, 180);
                font: normal normal 400 normal 16px / 25.6px 'Open Sans';
            }

            .content-paywall img {
                height: 60px;
            }
        """))
        return frag


class ContentTypeGatingPartitionScheme(object):
    """
    This scheme implements the Content Type Gating permission partitioning.

    This partitioning is roughly the same as the verified/audit split, but also allows for individual
    schools or courses to specify particular learner subsets by email that are allowed to access
    the gated content despite not being verified users.
    """

    LIMITED_ACCESS = Group(settings.CONTENT_TYPE_GATE_GROUP_IDS['limited_access'], 'Limited-access Users')
    FULL_ACCESS = Group(settings.CONTENT_TYPE_GATE_GROUP_IDS['full_access'], 'Full-access Users')

    @classmethod
    def get_group_for_user(cls, course_key, user, user_partition, **kwargs):
        """
        Returns the Group for the specified user.
        """

        # First, check if we have to deal with masquerading.
        # If the current user is masquerading as a specific student, use the
        # same logic as normal to return that student's group. If the current
        # user is masquerading as a generic student in a specific group, then
        # return that group.
        if get_course_masquerade(user, course_key) and not is_masquerading_as_specific_student(user, course_key):
            return get_masquerading_user_group(course_key, user, user_partition)

        # For now, treat everyone as a Full-access user, until we have the rest of the
        # feature gating logic in place.
        if not CONTENT_TYPE_GATING_FLAG.is_enabled():
            return cls.FULL_ACCESS

        # If CONTENT_TYPE_GATING is enabled use the following logic to determine whether a user should have FULL_ACCESS
        # or LIMITED_ACCESS

        course_mode = apps.get_model('course_modes.CourseMode')
        modes = course_mode.modes_for_course_dict(course_key)

        # If there is no verified mode, all users are granted FULL_ACCESS
        if not course_mode.has_verified_mode(modes):
            return cls.FULL_ACCESS

        course_enrollment = apps.get_model('student.CourseEnrollment')

        # TODO: remove this line
        # enrollment = course_enrollment.get_enrollment(user, course_key)

        mode_slug, is_active = course_enrollment.enrollment_mode_for_user(user, course_key)

        if mode_slug and is_active:
            course_mode = course_mode.mode_for_course(
                course_key,
                mode_slug,
                modes=course_mode.modes_for_course(course_key, include_expired=True, only_selectable=False),
            )
            if course_mode is None:
                LOG.error(
                    "User %s is in an unknown CourseMode '%s' for course %s. Granting full access to content for this user",
                    user.username,
                    mode_slug,
                    course_key,
                )
                return cls.FULL_ACCESS

            if mode_slug == COURSE_MODE_AUDIT:
                # TODO: does the below comment mean there is more work to be done here
                # Check the user email exceptions here:
                return cls.LIMITED_ACCESS
            else:
                return cls.FULL_ACCESS
        else:
            # Unenrolled users don't get gated content
            return cls.FULL_ACCESS

    @classmethod
    def create_user_partition(cls, id, name, description, groups=None, parameters=None, active=True):  # pylint: disable=redefined-builtin, invalid-name, unused-argument
        """
        Create a custom UserPartition to support dynamic groups.

        A Partition has an id, name, scheme, description, parameters, and a list
        of groups. The id is intended to be unique within the context where these
        are used. (e.g., for partitions of users within a course, the ids should
        be unique per-course). The scheme is used to assign users into groups.
        The parameters field is used to save extra parameters e.g., location of
        the course ID for this partition scheme.

        Partitions can be marked as inactive by setting the "active" flag to False.
        Any group access rule referencing inactive partitions will be ignored
        when performing access checks.
        """
        return ContentTypeGatingPartition(
            id,
            unicode(name),
            unicode(description),
            [
                cls.LIMITED_ACCESS,
                cls.FULL_ACCESS,
            ],
            cls,
            parameters,
            # N.B. This forces Content Type Gating to always be active on every course
            active=True,
        )


def _get_partition_from_id(partitions, user_partition_id):
    """
    Look for a user partition with a matching id in the provided list of partitions.

    Returns:
        A UserPartition, or None if not found.
    """
    for partition in partitions:
        if partition.id == user_partition_id:
            return partition

    return None
