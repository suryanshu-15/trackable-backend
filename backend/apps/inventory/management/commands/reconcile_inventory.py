"""
Verify that every package balance and location matches the movement ledger.
Exits non-zero when discrepancies exist, so it can run from cron / monitoring.
"""
import sys

from django.core.management.base import BaseCommand

from apps.inventory.services import reconciliation_issues


class Command(BaseCommand):
    help = 'Check package balances and locations against the inventory movement ledger.'

    def handle(self, *args, **options):
        issues = reconciliation_issues()
        if not issues:
            self.stdout.write(self.style.SUCCESS('Inventory reconciles with the movement ledger.'))
            return
        for issue in issues:
            self.stderr.write(f"{issue['package_id']}: {'; '.join(issue['problems'])}")
        self.stderr.write(self.style.ERROR(f'{len(issues)} package(s) do not reconcile.'))
        sys.exit(1)
