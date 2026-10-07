"""Mixin per dare ai campi dei form le classi Tailwind del tema scuro."""

from django import forms

CAMPO = (
    "block w-full rounded-lg border border-line bg-rail px-3 py-2 text-fg "
    "placeholder:text-muted/60 focus:border-indigo-400 focus:outline-none "
    "focus:ring-2 focus:ring-indigo-400/30"
)
FILE = (
    "block w-full rounded-lg border border-line bg-rail text-sm text-muted "
    "file:mr-4 file:border-0 file:bg-surface-2 file:px-4 file:py-2 file:text-sm "
    "file:font-medium file:text-fg hover:file:bg-line"
)


CASELLA = "size-4 rounded border-line bg-rail text-indigo-500 focus:ring-2 focus:ring-indigo-400/30"


class TailwindFormMixin:
    """Applica le classi del tema a tutti i widget (da mettere prima di forms.Form)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in self.fields.values():
            w = campo.widget
            if isinstance(w, forms.CheckboxInput):
                classe = CASELLA
            else:
                classe = FILE if isinstance(w, forms.ClearableFileInput) else CAMPO
            w.attrs["class"] = f"{w.attrs.get('class', '')} {classe}".strip()
