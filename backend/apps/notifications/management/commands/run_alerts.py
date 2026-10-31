"""
Scheduled alerts (spec §11): near-expiry / retest due, expired stock, overdue samples,
overdue put-away, low stock, ledger mismatches. Run from cron (e.g. hourly).
Each alert is sent at most once per recipient per entity per day.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.config import get_config
from apps.notifications.models import Notification
from apps.users.models import User


def _send(roles, *, title, message, level, entity_type, entity_id, link_url):
    start_of_day = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    sent = 0
    for user in User.objects.filter(is_active=True, role__in=roles):
        exists = Notification.objects.filter(recipient=user, title=title, entity_type=entity_type,
                                             entity_id=str(entity_id), created_at__gte=start_of_day).exists()
        if not exists:
            Notification.objects.create(recipient=user, title=title[:200], message=message, level=level,
                                        entity_type=entity_type, entity_id=str(entity_id), link_url=link_url)
            sent += 1
    return sent


def collect_alerts():
    """Yield alert dicts from current persisted state."""
    from apps.inventory import services as inv
    from apps.inventory.models import Package, PackageStatus
    from apps.laboratory.models import LabSample, ACTIVE_SAMPLE_STATUSES

    today = timezone.localdate()
    for pkg in inv.expiry_exposure(Package.objects.all()).select_related('material', 'unit'):
        expired = (pkg.expiry_date and pkg.expiry_date <= today) or (pkg.retest_date and pkg.retest_date < today)
        what = 'expired / retest overdue' if expired else 'expiring / retest due soon'
        yield {'roles': ('WAREHOUSE', 'LAB_REVIEWER'), 'level': 'ERROR' if expired else 'WARNING',
               'title': f'{pkg.package_id} {what}',
               'message': f'{pkg.material.code} lot {pkg.batch_number}: {pkg.current_quantity} {pkg.unit.symbol}, '
                          f'expiry {pkg.expiry_date or "-"}, retest {pkg.retest_date or "-"}',
               'entity_type': 'Package', 'entity_id': pkg.pk, 'link_url': f'/packages/{pkg.pk}'}
    for s in LabSample.objects.filter(status__in=ACTIVE_SAMPLE_STATUSES, required_by__lt=timezone.now()).select_related('material'):
        yield {'roles': ('LAB_TECH', 'LAB_REVIEWER'), 'level': 'WARNING', 'title': f'Sample {s.sample_id} overdue',
               'message': f'{s.material.code}: due {timezone.localtime(s.required_by):%Y-%m-%d %H:%M}, status {s.get_status_display()}',
               'entity_type': 'LabSample', 'entity_id': s.pk, 'link_url': f'/lab/samples/{s.pk}'}
    overdue_after = timezone.now() - timedelta(hours=int(get_config('alerts.putaway_overdue_hours')))
    for pkg in Package.objects.filter(status__in=[PackageStatus.CREATED, PackageStatus.AWAITING_PUTAWAY],
                                      created_at__lt=overdue_after, current_quantity__gt=0):
        yield {'roles': ('WAREHOUSE',), 'level': 'WARNING', 'title': f'Put-away overdue: {pkg.package_id}',
               'message': f'Created {timezone.localtime(pkg.created_at):%Y-%m-%d %H:%M}, still {pkg.get_status_display()}',
               'entity_type': 'Package', 'entity_id': pkg.pk, 'link_url': f'/packages/{pkg.pk}'}
    for row in inv.low_stock_materials():
        yield {'roles': ('WAREHOUSE', 'MANAGER'), 'level': 'WARNING', 'title': f"Low stock: {row['material_code']}",
               'message': f"Available {row['available']} {row['unit']} below minimum {row['minimum']}",
               'entity_type': 'Material', 'entity_id': row['material_id'], 'link_url': '/stock'}
    issues = inv.reconciliation_issues()
    if issues:
        yield {'roles': ('ADMIN',), 'level': 'CRITICAL', 'title': 'Inventory does not reconcile with the ledger',
               'message': f"{len(issues)} package(s), e.g. {issues[0]['package_id']}: {'; '.join(issues[0]['problems'])}",
               'entity_type': 'Reconciliation', 'entity_id': 'ledger', 'link_url': '/stock-control?tab=reconciliation'}


class Command(BaseCommand):
    help = 'Generate in-app alerts from current stock, laboratory and ledger state.'

    def handle(self, *args, **options):
        try:
            alerts = list(collect_alerts())
            sent = sum(_send(a.pop('roles'), **a) for a in alerts)
        except Exception as exc:
            _send(('ADMIN',), title='Scheduled job failed: run_alerts', message=repr(exc)[:1000], level='CRITICAL',
                  entity_type='Job', entity_id='run_alerts', link_url='')
            raise
        self.stdout.write(self.style.SUCCESS(f'{len(alerts)} alert(s) evaluated, {sent} notification(s) sent.'))
