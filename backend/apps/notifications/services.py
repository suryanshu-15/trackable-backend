"""
In-app notifications. Recipients are resolved by role at send time; each
notification links to the record it concerns (spec §11).
"""
from apps.notifications.models import Notification
from apps.users.models import User


def notify_roles(roles, *, title, message, level='INFO', entity_type='', entity_id='', link_url='', exclude=None):
    users = User.objects.filter(is_active=True, role__in=roles)
    if exclude is not None:
        users = users.exclude(pk=exclude.pk)
    Notification.objects.bulk_create([
        Notification(recipient=u, title=title[:200], message=message, level=level,
                     entity_type=entity_type, entity_id=str(entity_id), link_url=link_url)
        for u in users
    ])
