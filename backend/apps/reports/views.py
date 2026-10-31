"""
Reports, exports and the management dashboard (spec §4F, §12).
Exports reuse the report definitions, so they carry exactly the UI's filters and permissions.
"""
import csv
import io
import json
from datetime import timedelta

from django.conf import settings
from django.db.models import Avg, Count, F, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponse, StreamingHttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, OpenApiParameter
from openpyxl import Workbook
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import record_audit
from apps.core.exceptions import BusinessRuleViolation
from apps.core.models import AuditEvent
from apps.core.pagination import StandardResultsSetPagination
from apps.core.permissions import role_permission
from apps.inventory import services as inv
from apps.inventory.models import Package, MaterialIssue, MaterialIssueLine, StockAdjustment, DisposalRecord
from apps.laboratory.models import LabSample, LabTestRun, LabTestResult, QualityDisposition, QualityHold
from apps.receiving.models import GoodsReceipt, InvoiceLine, ReceiptLineVerification, VerificationStatus

from .registry import REPORTS, OVERSIGHT

XLSX_MAX_ROWS = getattr(settings, 'REPORT_XLSX_MAX_ROWS', 100_000)


def _cell(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


def _visible(user):
    return [r for r in REPORTS.values() if user.has_role(*r.roles)]


class ReportListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=['reports'], responses=OpenApiTypes.OBJECT, operation_id='reports_list')
    def get(self, request):
        """Reports available to the current user, with their filters."""
        return Response([{'key': r.key, 'title': r.title, 'description': r.description, 'filters': r.filters,
                          'exports': ['csv', 'xlsx']} for r in _visible(request.user)])


class _Echo:
    def write(self, value):
        return value


class ReportView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=['reports'], responses=OpenApiTypes.OBJECT, parameters=[
        OpenApiParameter('export', str, enum=['csv', 'xlsx'], description='Download instead of JSON'),
        OpenApiParameter('page', int), OpenApiParameter('page_size', int)])
    def get(self, request, key):
        report = REPORTS.get(key)
        if report is None:
            raise BusinessRuleViolation('Unknown report.', code='not_found', status_code=404)
        if not request.user.has_role(*report.roles):
            raise BusinessRuleViolation('Your role cannot run this report.', code='forbidden', status_code=403)
        params = {k: v for k, v in request.query_params.items() if k in report.filters}
        rows = report.rows(params)
        columns = report.get_columns()
        headers = [h for h, _ in columns]
        export = request.query_params.get('export')
        if export:
            record_audit(request.user, AuditEvent.ACTION_DOWNLOAD, 'Report', key,
                         extra={'format': export, 'filters': params}, request=request)
        filename = f"{key}-{timezone.localdate().isoformat()}"

        def values(row):
            return [_cell(fn(row)) for _, fn in columns]

        def iterate():
            if hasattr(rows, 'iterator'):
                return rows.iterator(chunk_size=2000)
            return iter(rows)

        if export == 'csv':
            writer = csv.writer(_Echo())

            def stream():
                yield '﻿'  # BOM so spreadsheet apps detect UTF-8
                yield writer.writerow(headers)
                for row in iterate():
                    yield writer.writerow(values(row))

            response = StreamingHttpResponse(stream(), content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="{filename}.csv"'
            return response

        if export == 'xlsx':
            total = rows.count() if hasattr(rows, 'count') and not isinstance(rows, list) else len(rows)
            if total > XLSX_MAX_ROWS:
                raise BusinessRuleViolation(f'{total} rows exceed the Excel limit of {XLSX_MAX_ROWS}; narrow the '
                                            'filters or export CSV.', code='export_too_large')
            wb = Workbook(write_only=True)
            ws = wb.create_sheet(report.title[:31])
            ws.append(headers)
            for row in iterate():
                ws.append(values(row))
            buf = io.BytesIO()
            wb.save(buf)
            response = HttpResponse(buf.getvalue(),
                                    content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = f'attachment; filename="{filename}.xlsx"'
            return response

        if export:
            raise BusinessRuleViolation('export must be csv or xlsx.')

        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(rows if not isinstance(rows, list) else rows, request, view=self)
        response = paginator.get_paginated_response([[fn(r) for _, fn in columns] for r in page])
        response.data = {'report': key, 'title': report.title, 'columns': headers, 'filters': params, **response.data}
        return response


def _hours(td):
    return round(td.total_seconds() / 3600, 1) if td else None


class ManagementDashboardView(APIView):
    permission_classes = [role_permission(*OVERSIGHT)]

    @extend_schema(tags=['reports'], responses=OpenApiTypes.OBJECT, parameters=[OpenApiParameter('days', int)])
    def get(self, request):
        """Management KPIs over the last `days` (default 30), computed from persisted records."""
        try:
            days = max(1, min(int(request.query_params.get('days', 30)), 366))
        except ValueError:
            raise BusinessRuleViolation('days must be a number.')
        since = timezone.now() - timedelta(days=days)
        receipts = GoodsReceipt.objects.exclude(status='CANCELLED').filter(arrival_datetime__gte=since)
        lines = InvoiceLine.objects.filter(receipt__in=receipts)
        results = LabTestResult.objects.filter(entered_at__gte=since, test_run__is_current=True)
        result_counts = dict(results.values_list('result_status').annotate(c=Count('id')))
        decided = sum(result_counts.values())
        issue_lines = MaterialIssueLine.objects.filter(issue__issue_datetime__gte=since)
        in_wh = Package.objects.in_warehouse()

        return Response({
            'period_days': days,
            'inward': {
                'receipts': receipts.count(),
                'by_day': [{'date': r['d'], 'receipts': r['c']} for r in
                           receipts.annotate(d=TruncDate('arrival_datetime')).values('d').annotate(c=Count('id')).order_by('d')],
                'by_supplier': list(receipts.values('supplier__code', 'supplier__name').annotate(receipts=Count('id'))
                                    .order_by('-receipts')[:10]),
                'by_material': [{**r, 'quantity': str(r['quantity'])} for r in
                                lines.values('material__code', 'declared_unit__symbol')
                                .annotate(quantity=Sum('declared_quantity'), lines=Count('id')).order_by('-lines')[:10]],
            },
            'quality': {
                'decisions': dict(QualityDisposition.objects.filter(decided_at__gte=since)
                                  .values_list('decision').annotate(c=Count('id'))),
                'test_results': result_counts,
                'test_pass_rate_percent': round(result_counts.get('PASS', 0) / decided * 100, 1) if decided else None,
                'avg_lab_turnaround_hours': _hours(LabSample.objects.filter(completed_at__gte=since).aggregate(
                    a=Avg(F('completed_at') - F('created_at')))['a']),
                'avg_testing_hours': _hours(LabTestRun.objects.filter(submitted_at__gte=since).aggregate(
                    a=Avg(F('submitted_at') - F('started_at')))['a']),
                'samples_open': LabSample.objects.filter(status__in=['REQUESTED', 'COLLECTED', 'RECEIVED_BY_LAB', 'TESTING',
                                                                     'RESULTS_SUBMITTED', 'RETEST_REQUIRED']).count(),
            },
            'flow': {
                'avg_gate_to_putaway_hours': _hours(receipts.filter(stored_at__isnull=False).aggregate(
                    a=Avg(F('stored_at') - F('arrival_datetime')))['a']),
                'avg_unloading_hours': _hours(receipts.filter(unloading_completed_at__isnull=False).aggregate(
                    a=Avg(F('unloading_completed_at') - F('unloading_started_at')))['a']),
            },
            'stock': {
                'packages_in_warehouse': in_wh.count(),
                'by_qc_status': dict(in_wh.values_list('qc_status').annotate(c=Count('id'))),
                'available_packages': Package.objects.available().count(),
                'ageing_package_counts': _ageing_counts(in_wh),
                'near_expiry_or_retest': inv.expiry_exposure(Package.objects.all()).count(),
                'low_stock_materials': len(inv.low_stock_materials()),
            },
            'issues': {
                'issues': MaterialIssue.objects.filter(issue_datetime__gte=since).count(),
                'by_day': [{'date': r['d'], 'issues': r['c']} for r in
                           MaterialIssue.objects.filter(issue_datetime__gte=since).annotate(d=TruncDate('issue_datetime'))
                           .values('d').annotate(c=Count('id')).order_by('d')],
                'by_material': [{**r, 'quantity': str(r['quantity'])} for r in
                                issue_lines.values('material__code', 'unit__symbol')
                                .annotate(quantity=Sum('issued_quantity')).order_by('-quantity')[:10]],
            },
            'reconciliation': {
                'ledger_mismatches': len(inv.reconciliation_issues()),
                'open_receipt_discrepancies': ReceiptLineVerification.objects.filter(
                    verification_status=VerificationStatus.DISCREPANT).count(),
            },
            'exceptions': {
                'fifo_overrides': MaterialIssue.objects.filter(issue_datetime__gte=since, fifo_override=True).count(),
                'adjustments_approved': StockAdjustment.objects.filter(approved_at__gte=since, status='APPROVED').count(),
                'adjustments_pending': StockAdjustment.objects.filter(status='PENDING').count(),
                'disposals': DisposalRecord.objects.filter(created_at__gte=since).count(),
                'weight_corrections': AuditEvent.objects.filter(timestamp__gte=since, extra__kind='WEIGHT_CORRECTION').count(),
                'discrepancies_accepted': AuditEvent.objects.filter(
                    timestamp__gte=since, entity_type='ReceiptLineVerification', action=AuditEvent.ACTION_APPROVE).count(),
                'active_holds': QualityHold.objects.filter(is_active=True, hold_type='HOLD').count(),
                'active_recalls': QualityHold.objects.filter(is_active=True, hold_type='RECALL').count(),
            },
        })


def _ageing_counts(qs):
    today = timezone.localdate()
    buckets = inv.ageing_buckets()
    counts = {label: 0 for label, _, _ in buckets}
    for received in qs.values_list('received_at', flat=True):
        age = (today - timezone.localtime(received).date()).days
        counts[next(label for label, _, upper in buckets if upper is None or age <= upper)] += 1
    return counts


# Kept for API compatibility with the original endpoint names
class PackageLifecycleView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=['reports'], responses=OpenApiTypes.OBJECT)
    def get(self, request, package_id=None):
        """Full lifecycle timeline for a package, by its printed package ID."""
        pkg = Package.objects.filter(package_id__iexact=package_id).first()
        if pkg is None:
            raise BusinessRuleViolation('Package not found.', code='not_found', status_code=404)
        return Response({'package_id': pkg.package_id, 'events': inv.package_timeline(pkg)})



def _series_stats(by_day, days):
    """Daily counts → average over the whole period (idle days count as 0), busiest day."""
    total = sum(r['count'] for r in by_day)
    peak = max(by_day, key=lambda r: r['count'], default=None)
    return {'total': total, 'per_day_avg': round(total / days, 2), 'peak_day': peak, 'by_day': by_day}


class UsageAnalysisView(APIView):
    """
    Measured usage and volumes (spec §13 / DECISIONS Q10): who uses the system, how much flows
    through it and how many people work at the same time. Observations only — no targets.
    """
    permission_classes = [role_permission(*OVERSIGHT)]

    @extend_schema(tags=['reports'], responses=OpenApiTypes.OBJECT, parameters=[OpenApiParameter('days', int)])
    def get(self, request):
        from django.db.models.functions import ExtractHour, TruncHour
        from apps.inventory.models import InventoryMovement
        from apps.master.models import StorageLocation, Warehouse
        from apps.users.models import User

        try:
            days = max(1, min(int(request.query_params.get('days', 30)), 366))
        except ValueError:
            raise BusinessRuleViolation('days must be a number.')
        since = timezone.now() - timedelta(days=days)
        events = AuditEvent.objects.filter(timestamp__gte=since, actor__isnull=False)

        def daily(qs, field):
            return [{'date': r['d'], 'count': r['c']} for r in
                    qs.filter(**{f'{field}__gte': since}).annotate(d=TruncDate(field)).values('d')
                    .annotate(c=Count('id')).order_by('d')]

        # Concurrency: distinct people acting within the same clock hour.
        hourly = list(events.annotate(h=TruncHour('timestamp')).values('h')
                      .annotate(people=Count('actor', distinct=True), actions=Count('id')).order_by('h'))
        peak_hour = max(hourly, key=lambda r: (r['people'], r['actions']), default=None)
        by_hour_of_day = {h: 0 for h in range(24)}
        for r in events.annotate(h=ExtractHour('timestamp')).values('h').annotate(c=Count('id')):
            by_hour_of_day[r['h']] = r['c']

        users = User.objects.filter(is_active=True)
        acting = events.values('actor').distinct()
        return Response({
            'period_days': days,
            'note': 'Measured from recorded activity; no targets are applied.',
            'users': {
                'active_accounts': users.count(),
                'by_role': dict(users.values_list('role').annotate(c=Count('id'))),
                'active_in_period': acting.count(),
                'active_in_period_by_role': dict(events.values_list('actor__role')
                                                 .annotate(c=Count('actor', distinct=True))),
                'signed_in_in_period': users.filter(last_login__gte=since).count(),
                'actions_per_role': dict(events.values_list('actor__role').annotate(c=Count('id'))),
            },
            'concurrency': {
                'peak_people_in_one_hour': peak_hour['people'] if peak_hour else 0,
                'peak_hour': peak_hour['h'] if peak_hour else None,
                'avg_people_per_active_hour': round(sum(r['people'] for r in hourly) / len(hourly), 2) if hourly else 0,
                'active_hours': len(hourly),
                'actions_by_hour_of_day': by_hour_of_day,
            },
            'volumes': {
                'receipts': _series_stats(daily(GoodsReceipt.objects.exclude(status='CANCELLED'), 'arrival_datetime'), days),
                'bags_created': _series_stats(daily(Package.objects.filter(parent_package__isnull=True), 'created_at'), days),
                'samples': _series_stats(daily(LabSample.objects.all(), 'created_at'), days),
                'issues': _series_stats(daily(MaterialIssue.objects.all(), 'issue_datetime'), days),
                'stock_movements': _series_stats(daily(InventoryMovement.objects.all(), 'created_at'), days),
                'scans': _series_stats(daily(AuditEvent.objects.filter(action=AuditEvent.ACTION_SCAN), 'timestamp'), days),
                'label_prints': _series_stats(daily(AuditEvent.objects.filter(action=AuditEvent.ACTION_PRINT), 'timestamp'), days),
            },
            'footprint': {
                'warehouses': Warehouse.objects.filter(is_active=True).count(),
                'storage_locations': StorageLocation.objects.filter(is_active=True).count(),
                'bags_in_stock': Package.objects.in_warehouse().count(),
                'records': {'packages': Package.objects.count(), 'movements': InventoryMovement.objects.count(),
                            'audit_events': AuditEvent.objects.count(), 'lab_samples': LabSample.objects.count()},
            },
        })
