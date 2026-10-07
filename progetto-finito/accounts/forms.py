from django.contrib.auth.forms import AuthenticationForm

_CAMPO = (
    "block w-full rounded-lg border border-line bg-rail px-3 py-2 text-fg "
    "placeholder:text-muted/60 focus:border-indigo-400 focus:outline-none "
    "focus:ring-2 focus:ring-indigo-400/30"
)


class LoginForm(AuthenticationForm):
    """Form di accesso con le classi Tailwind sui campi."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Nome utente"
        self.fields["username"].widget.attrs.update(
            {"class": _CAMPO, "autocomplete": "username", "autofocus": True}
        )
        self.fields["password"].widget.attrs.update(
            {"class": _CAMPO, "autocomplete": "current-password"}
        )
