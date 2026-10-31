"""
Global search (spec §4G): receipt, invoice, truck, supplier, material, package ID,
barcode / QR content, sample ID, batch / lot, issue number and production order.
Results respect the same role / object scoping as the detail endpoints.
"""
from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import BusinessRuleViolation

LIMIT = 10
RECEIPT_READERS = ('ADMIN', 'GATE', 'RECEIVING', 'WAREHOUSE', 'LAB_TECH', 'LAB_REVIEWER', 'AUDITOR', 'MANAGER')


def global_search(user, q):
    from apps.inventory.models import Package, MaterialIssue, MaterialRequest
    from apps.laboratory.models import LabSample
    from apps.master.models import Material, Supplier
    from apps.receiving.models import GoodsReceipt

    term = q.strip()
    # A scanned QR URL resolves to its token
    token = term.rstrip('/').rsplit('/scan/', 1)[-1] if '/scan/' in term else None
    out = {}
    if user.has_role(*RECEIPT_READERS):
        out['receipts'] = [{'id': str(r.pk), 'label': r.receipt_number, 'detail': f'{r.supplier.name} · {r.invoice_number} · {r.vehicle_number}',
                            'status': r.status}
                           for r in GoodsReceipt.objects.select_related('supplier').filter(
                               Q(receipt_number__icontains=term) | Q(invoice_number__icontains=term)
                               | Q(vehicle_number__icontains=term) | Q(supplier__name__icontains=term)
                               | Q(purchase_order_number__icontains=term)).order_by('-arrival_datetime')[:LIMIT]]
    pkg_q = Q(package_id__icontains=term) | Q(batch_number__icontains=term)
    if token:
        pkg_q |= Q(scan_token=token)
    elif len(term) >= 20:
        pkg_q |= Q(scan_token=term)
    out['packages'] = [{'id': str(p.pk), 'label': p.package_id, 'detail': f'{p.material.code} · {p.batch_number} · '
                        f'{p.current_quantity} {p.unit.symbol}', 'status': p.status, 'qc_status': p.qc_status}
                       for p in Package.objects.select_related('material', 'unit').filter(pkg_q).order_by('-created_at')[:LIMIT]]
    out['samples'] = [{'id': str(s.pk), 'label': s.sample_id, 'detail': f'{s.material.code} · {s.batch_number}', 'status': s.status}
                      for s in LabSample.objects.select_related('material').filter(
                          Q(sample_id__icontains=term) | Q(batch_number__icontains=term)).order_by('-created_at')[:LIMIT]]
    issues = MaterialIssue.objects.select_related('factory').filter(
        Q(issue_number__icontains=term) | Q(production_order__icontains=term) | Q(reference__icontains=term))
    requests = MaterialRequest.objects.select_related('factory').filter(
        Q(request_number__icontains=term) | Q(production_order__icontains=term))
    if user.has_role('FACTORY') and not user.is_superuser:
        issues = issues.filter(Q(material_request__requested_by=user) | Q(received_by=user))
        requests = requests.filter(requested_by=user)
    out['issues'] = [{'id': str(i.pk), 'label': i.issue_number, 'detail': f'{i.factory.name} · {i.production_order}', 'status': i.status}
                     for i in issues.order_by('-issue_datetime')[:LIMIT]]
    out['requests'] = [{'id': str(r.pk), 'label': r.request_number, 'detail': f'{r.factory.name} · {r.production_order}',
                        'status': r.status} for r in requests.order_by('-created_at')[:LIMIT]]
    out['materials'] = [{'id': m.pk, 'label': m.code, 'detail': m.name}
                        for m in Material.objects.filter(Q(code__icontains=term) | Q(name__icontains=term))[:LIMIT]]
    out['suppliers'] = [{'id': s.pk, 'label': s.code, 'detail': s.name}
                        for s in Supplier.objects.filter(Q(code__icontains=term) | Q(name__icontains=term))[:LIMIT]]
    return out


class GlobalSearchView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=['search'], parameters=[OpenApiParameter('q', str, required=True)], responses=OpenApiTypes.OBJECT)
    def get(self, request):
        q = request.query_params.get('q', '')
        if len(q.strip()) < 2:
            raise BusinessRuleViolation('Enter at least 2 characters.', details={'q': ['Too short.']})
        return Response({'query': q, 'results': global_search(request.user, q)})
