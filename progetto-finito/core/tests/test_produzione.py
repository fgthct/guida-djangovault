"""Fase 10: configurazione di produzione (Django, Nginx, compose, script). Non serve Docker."""

import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

RADICE = Path(settings.BASE_DIR)


class ControlliDjangoTests(SimpleTestCase):
    def test_connessioni_al_database_non_persistenti(self):
        """Sotto ASGI le connessioni persistenti non vengono chiuse da nessuno e finiscono i posti di Postgres."""
        self.assertEqual(settings.DATABASES["default"]["CONN_MAX_AGE"], 0)

    def test_check_deploy_senza_avvisi_a_parte_hsts(self):
        """Con le variabili di produzione `check --deploy` non deve trovare altro che l'HSTS (che si attiva a mano)."""
        env = {
            **os.environ,
            "DJANGO_DEBUG": "0",
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(60),
            "DJANGO_ALLOWED_HOSTS": "dms.example.org",
            "DJANGO_CSRF_TRUSTED_ORIGINS": "https://dms.example.org",
            "DJANGO_ADMIN_PATH": "gestione-x7k2/",
        }
        r = subprocess.run(
            [sys.executable, "manage.py", "check", "--deploy"], cwd=RADICE, env=env, capture_output=True, text=True, timeout=120
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        trovati = set(re.findall(r"security\.W\d+|\(\w+\.[WEC]\d+\)", r.stderr + r.stdout))
        self.assertLessEqual(trovati, {"security.W004", "(security.W004)"}, r.stderr)

    def test_senza_chiave_segreta_in_produzione_non_parte(self):
        env = {**os.environ, "DJANGO_DEBUG": "0", "DJANGO_SECRET_KEY": "", "DJANGO_ALLOWED_HOSTS": "x.example.org"}
        r = subprocess.run([sys.executable, "manage.py", "check"], cwd=RADICE, env=env, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", r.stderr)

    def test_il_controllo_di_salute_non_viene_reindirizzato_a_https(self):
        # In produzione (DEBUG spento) il controllo di Docker arriva in HTTP direttamente all'app.
        self.assertIn('SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]', (RADICE / "config/settings.py").read_text())


class NginxTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.locations = (RADICE / "deploy/nginx/locations.conf").read_text()

    def blocchi(self):
        """(intestazione della location, corpo) per ciascuna location."""
        return re.findall(r"location\s+([^{]+)\{(.*?)\n\}", self.locations, flags=re.S)

    def test_ogni_location_che_fa_proxy_include_le_intestazioni_comuni(self):
        # In Nginx una location con una sua proxy_set_header perde Host e X-Forwarded-* del server.
        con_proxy = [(h, c) for h, c in self.blocchi() if "proxy_pass" in c]
        self.assertGreaterEqual(len(con_proxy), 4)
        for intestazione, corpo in con_proxy:
            self.assertIn("dv-proxy.conf", corpo, intestazione)
        self.assertNotIn("proxy_set_header Host", self.locations)

    def test_intestazioni_fissate_da_nginx(self):
        proxy = (RADICE / "deploy/nginx/proxy.conf").read_text()
        self.assertIn("X-Forwarded-For   $remote_addr", proxy)  # sovrascrive: il client non può falsificarlo
        self.assertIn("Host              $http_host", proxy)  # con la porta
        self.assertIn("X-Forwarded-Proto $dv_scheme", proxy)

    def test_flusso_ws_e_media(self):
        self.assertRegex(self.locations, r"flusso/\$\s*\{[^}]*proxy_buffering off")
        self.assertRegex(self.locations, r"location /ws/ \{[^}]*Upgrade \$http_upgrade[^}]*\"upgrade\"")
        self.assertRegex(self.locations, r"location /media/ \{ return 404; \}")
        self.assertIn("limit_req zone=dv_login", self.locations)

    def test_template_http_e_https(self):
        http = (RADICE / "deploy/nginx/http.conf.template").read_text()
        https = (RADICE / "deploy/nginx/https.conf.template").read_text()
        for t in (http, https):
            self.assertIn("client_max_body_size ${DV_NGINX_MAX_BODY}", t)
            self.assertIn("dv-locations.conf", t)
            self.assertIn("/nginx-health", t)
        self.assertIn("ssl_protocols TLSv1.2 TLSv1.3", https)
        self.assertIn("return 301 https://", https)
        self.assertNotIn("ssl_certificate", http)

    def test_limite_di_nginx_non_inferiore_a_quello_dell_app(self):
        env = (RADICE / ".env.prod.example").read_text()
        corpo = int(re.search(r"^DV_NGINX_MAX_BODY=(\d+)m", env, flags=re.M).group(1))
        app = int(re.search(r"^DOCUMENTS_MAX_UPLOAD_MB=(\d+)", env, flags=re.M).group(1))
        self.assertGreater(corpo, app)


class ComposeETestiTests(SimpleTestCase):
    def test_compose_prod_non_espone_db_e_redis(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML non installato")
        c = yaml.safe_load((RADICE / "compose.prod.yml").read_text())
        s = c["services"]
        self.assertNotIn("ports", s["postgres"])
        self.assertNotIn("ports", s["redis"])
        self.assertIn("daphne", s["django"]["command"])
        self.assertIn("--proxy-headers", s["django"]["command"])
        self.assertIn("collectstatic", s["django"]["command"])
        for nome in ("django", "worker"):
            self.assertEqual(s[nome]["user"], "1000:1000")
            self.assertFalse(any(str(v).startswith(".:") for v in s[nome]["volumes"]), "niente bind mount del codice")
        self.assertIn("./inbox:/app/inbox", s["django"]["volumes"])  # cartella di arrivo per importa_cartella
        # il worker osserva la cartella di arrivo (ingestione automatica) e scrive i documenti nei media
        self.assertIn("./inbox:/app/inbox", s["worker"]["volumes"])
        self.assertTrue(any(str(v).startswith("media_data:") for v in s["worker"]["volumes"]))
        self.assertEqual(c["name"], "djangovault-prod")  # volumi separati da quelli di sviluppo

    def test_script_con_sintassi_valida(self):
        for nome in ("prod.sh", "up.sh", "check_ports.sh", "installa_backup.sh", "scarica_backup.sh",
                     "invio_automatico.sh", "installa_invio.sh", "certificato.sh"):
            r = subprocess.run(["bash", "-n", str(RADICE / "scripts" / nome)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{nome}: {r.stderr}")

    def test_esempio_di_ambiente_di_produzione_completo(self):
        chiavi = set(re.findall(r"^([A-Z_]+)=", (RADICE / ".env.prod.example").read_text(), flags=re.M))
        for k in ("DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS", "DJANGO_CSRF_TRUSTED_ORIGINS", "DB_PASSWORD", "DV_NGINX_MODE",
                  "DV_SERVER_NAME", "AUDIT_RETENTION_DAYS", "NOTIFICATION_RETENTION_DAYS"):
            self.assertIn(k, chiavi)
        self.assertNotIn("DJANGO_DEBUG", chiavi)  # in produzione lo imposta compose.prod.yml a 0
