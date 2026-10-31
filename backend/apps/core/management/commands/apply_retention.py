"""
Retention (spec §13, agreed periods in docs/DECISIONS.md): audit events and receipt documents
are kept `retention.audit_days` / `retention.document_days` (1 year); finished lab samples with
their runs, results, decisions and reports `retention.lab_days` (3 years).

Dry run by default — prints what would be removed. `--apply` deletes, removes the stored files
and records one audit event describing the purge. Run it on a restored copy first.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.config import get_config
from apps.core.models import AuditEvent
from apps.laboratory.models import (
    LabSample, LabSampleAttachment, LabTestResult, LabTestRun, QualityDisposition, SamplePackage, SampleStatus,
)
from apps.receiving.models import ReceiptDocument


def retention_sets(now=None):
    now = now or timezone.now()
    audit_cutoff = now - timedelta(days=int(get_config('retention.audit_days')))
    doc_cutoff = now - timedelta(days=int(get_config('retention.document_days')))
    lab_cutoff = now - timedelta(days=int(get_config('retention.lab_days')))
    samples = LabSample.objects.filter(status__in=[SampleStatus.COMPLETED, SampleStatus.CANCELLED],
                                       updated_at__lt=lab_cutoff)
    return {
        'audit_events': AuditEvent.objects.filter(timestamp__lt=audit_cutoff),
        'receipt_documents': ReceiptDocument.objects.filter(created_at__lt=doc_cutoff),
        'lab_samples': samples,
    }


class Command(BaseCommand):
    help = 'Apply the agreed retention periods (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Delete instead of only reporting.')

    def handle(self, *args, apply=False, **options):
        sets = retention_sets()
        counts = {name: qs.count() for name, qs in sets.items()}
        for name, n in counts.items():
            self.stdout.write(f'{name}: {n} past retention')
        if not apply:
            self.stdout.write('Dry run — nothing deleted. Re-run with --apply.')
            return

        files = []
        with transaction.atomic():
            for doc in sets['receipt_documents']:
                files.append(doc.file)
            sets['receipt_documents'].delete()

            sample_ids = list(sets['lab_samples'].values_list('pk', flat=True))
            attachments = LabSampleAttachment.objects.filter(sample_id__in=sample_ids)
            files += [a.file for a in attachments]
            attachments.delete()
            # Children first: every lab relation is PROTECT so nothing disappears by accident.
            QualityDisposition.objects.filter(sample_id__in=sample_ids).delete()
            LabTestResult.objects.filter(test_run__sample_id__in=sample_ids).delete()
            LabTestRun.objects.filter(sample_id__in=sample_ids).delete()
            SamplePackage.objects.filter(sample_id__in=sample_ids).delete()
            LabSample.objects.filter(pk__in=sample_ids).delete()

            sets['audit_events'].delete()
            record_audit(None, AuditEvent.ACTION_DELETE, 'Retention', timezone.localdate().isoformat(),
                         reason='Scheduled retention purge', after={k: str(v) for k, v in counts.items()},
                         extra={'periods_days': {k: get_config(f'retention.{k}_days') for k in ('audit', 'document', 'lab')}})

        # Files go only after the database commit succeeded.
        for f in files:
            try:
                f.storage.delete(f.name)
            except OSError as exc:
                self.stderr.write(f'Could not delete {f.name}: {exc}')
        self.stdout.write(self.style.SUCCESS(f'Retention applied: {counts}'))
