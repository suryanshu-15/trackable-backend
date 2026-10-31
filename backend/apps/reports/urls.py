from django.urls import path
from .views import ReportListView, ReportView, ManagementDashboardView, UsageAnalysisView, PackageLifecycleView

urlpatterns = [
    path('', ReportListView.as_view(), name='report-list'),
    path('management-dashboard/', ManagementDashboardView.as_view(), name='report-management'),
    path('usage-analysis/', UsageAnalysisView.as_view(), name='report-usage'),
    path('package-lifecycle/<str:package_id>/', PackageLifecycleView.as_view(), name='report-package-lifecycle'),
    path('<slug:key>/', ReportView.as_view(), name='report-detail'),
]
