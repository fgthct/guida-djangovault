from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0005_revisione_a_piu_persone"),
    ]

    operations = [
        migrations.AddField(
            model_name="reviewround",
            name="message",
            field=models.TextField(blank=True, max_length=1000, verbose_name="messaggio ai revisori"),
        ),
    ]
