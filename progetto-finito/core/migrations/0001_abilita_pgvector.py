from django.contrib.postgres.operations import CreateExtension
from django.db import migrations


class Migration(migrations.Migration):
    """Abilita l'estensione pgvector: da qui in poi i model possono avere campi vettoriali."""

    dependencies = []

    operations = [CreateExtension("vector")]
