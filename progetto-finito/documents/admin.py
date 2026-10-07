from django.contrib import admin

from .models import Category, Document, DocumentPermission, DocumentVersion, ReviewRound, ReviewStep


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    search_fields = ["name"]


class VersionInline(admin.TabularInline):
    model = DocumentVersion
    extra = 0
    readonly_fields = [
        "version_number", "original_name", "size", "checksum", "comment", "created_by", "created_at",
        "processing_status", "page_count", "ocr_used",
    ]
    fields = readonly_fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class PermissionInline(admin.TabularInline):
    model = DocumentPermission
    extra = 0
    fields = ["user", "group", "level", "granted_by"]
    raw_id_fields = ["user", "granted_by"]


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ["title", "category", "owner", "status", "updated_at"]
    list_filter = ["status", "category"]
    search_fields = ["title", "description"]
    inlines = [VersionInline, PermissionInline]


class StepInline(admin.TabularInline):
    model = ReviewStep
    extra = 0
    can_delete = False
    readonly_fields = ["position", "reviewer", "label", "decision", "decided_at", "note"]
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ReviewRound)
class ReviewRoundAdmin(admin.ModelAdmin):
    list_display = ["document", "number", "sequential", "outcome", "started_by", "started_at"]
    list_filter = ["outcome", "sequential"]
    inlines = [StepInline]


@admin.action(description="Rimetti in coda l'elaborazione (testo, OCR, anteprima)")
def rimetti_in_coda(modeladmin, request, queryset):
    from .services import processing

    n = 0
    for v in queryset:
        try:
            processing.richiedi_rielaborazione(user=request.user, version=v)
            n += 1
        except processing.ElaborazioneInCorso:
            pass
    modeladmin.message_user(request, f"{n} versioni rimesse in coda.")


@admin.register(DocumentVersion)
class DocumentVersionAdmin(admin.ModelAdmin):
    list_display = ["__str__", "original_name", "processing_status", "page_count", "ocr_used", "processed_at"]
    list_filter = ["processing_status", "ocr_used"]
    search_fields = ["original_name", "document__title"]
    actions = [rimetti_in_coda]
    readonly_fields = [f.name for f in DocumentVersion._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False  # le versioni si eliminano con il documento (i file vanno rimossi dal servizio)

    def has_change_permission(self, request, obj=None):
        return False
