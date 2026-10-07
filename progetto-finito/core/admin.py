from django.contrib import admin

from .models import AuditLog, Notification


class SoloLettura(admin.ModelAdmin):
    """Sola consultazione: il registro non si modifica né si cancella, nemmeno dall'admin."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(SoloLettura):
    list_display = ["timestamp", "username", "action", "object_repr", "ip_address"]
    list_filter = ["action"]
    search_fields = ["username", "object_repr", "action"]
    date_hierarchy = "timestamp"


@admin.register(Notification)
class NotificationAdmin(SoloLettura):
    list_display = ["created_at", "recipient", "kind", "message", "read_at"]
    list_filter = ["kind"]
