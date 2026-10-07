from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse

from accounts.models import User
from documents import permissions
from documents.models import Document, DocumentPermission
from documents.services import CondivisioneNonValida, revoke_share, share_document

from .base import DocumentTestCase

L = DocumentPermission.Level
HTMX = {"HTTP_HX_REQUEST": "true"}


class PermBase(DocumentTestCase):
    def setUp(self):
        self.doc = self.nuovo_documento(owner=self.mario, titolo="Di Mario")

    def dai(self, user=None, group=None, level=L.READ):
        return DocumentPermission.objects.create(document=self.doc, user=user, group=group, level=level)


class ModelloTests(PermBase):
    def test_serve_esattamente_uno_tra_utente_e_gruppo(self):
        g = Group.objects.create(name="Legale")
        for kw in ({}, {"user": self.luisa, "group": g}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                DocumentPermission.objects.create(document=self.doc, level=L.READ, **kw)

    def test_una_sola_condivisione_per_destinatario(self):
        self.dai(user=self.luisa)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.dai(user=self.luisa, level=L.WRITE)

    def test_eliminando_il_documento_spariscono_le_condivisioni(self):
        self.dai(user=self.luisa)
        self.doc.delete()
        self.assertEqual(DocumentPermission.objects.count(), 0)


class LivelloTests(PermBase):
    def test_proprietario_e_superutente_sono_admin(self):
        self.assertEqual(permissions.livello(self.mario, self.doc), L.ADMIN)
        self.assertEqual(permissions.livello(self.admin, self.doc), L.ADMIN)

    def test_estraneo_e_anonimo_nessun_accesso(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(permissions.livello(self.luisa, self.doc), 0)
        self.assertEqual(permissions.livello(AnonymousUser(), self.doc), 0)

    def test_livello_diretto(self):
        self.dai(user=self.luisa, level=L.WRITE)
        self.assertEqual(permissions.livello(self.luisa, self.doc), L.WRITE)

    def test_livello_tramite_gruppo(self):
        g = Group.objects.create(name="Legale")
        self.luisa.groups.add(g)
        self.dai(group=g, level=L.READ)
        self.assertEqual(permissions.livello(self.luisa, self.doc), L.READ)
        self.assertEqual(permissions.livello(self.staff, self.doc), 0)  # non nel gruppo

    def test_vale_il_livello_piu_alto_tra_diretto_e_gruppo(self):
        g = Group.objects.create(name="Legale")
        self.luisa.groups.add(g)
        self.dai(group=g, level=L.ADMIN)
        self.dai(user=self.luisa, level=L.READ)
        self.assertEqual(permissions.livello(self.luisa, self.doc), L.ADMIN)

    def test_matrice_dei_permessi(self):
        attesi = {
            L.READ: dict(view=True, download=True, edit=False, version=False, restore=False, delete=False, share=False),
            L.WRITE: dict(view=True, download=True, edit=True, version=True, restore=True, delete=False, share=False),
            L.ADMIN: dict(view=True, download=True, edit=True, version=True, restore=True, delete=True, share=True),
        }
        for livello, atteso in attesi.items():
            doc = self.nuovo_documento(owner=self.mario, titolo=f"L{livello}", uploaded_file=self.file(contenuto=str(livello).encode()))
            DocumentPermission.objects.create(document=doc, user=self.luisa, level=livello)
            u = self.luisa
            trovato = dict(
                view=permissions.can_view(u, doc), download=permissions.can_download(u, doc),
                edit=permissions.can_edit(u, doc), version=permissions.can_add_version(u, doc),
                restore=permissions.can_restore_version(u, doc), delete=permissions.can_delete(u, doc),
                share=permissions.can_share(u, doc),
            )
            self.assertEqual(trovato, atteso, L(livello).label)


class VisibleToTests(PermBase):
    def test_condivisione_diretta_e_di_gruppo_rendono_visibile(self):
        self.assertFalse(Document.objects.visible_to(self.luisa).exists())
        self.dai(user=self.luisa)
        self.assertEqual(list(Document.objects.visible_to(self.luisa)), [self.doc])
        g = Group.objects.create(name="G")
        self.staff.groups.add(g)
        self.dai(group=g)
        self.assertEqual(list(Document.objects.visible_to(self.staff)), [self.doc])

    def test_nessun_duplicato_con_piu_accessi(self):
        g1, g2 = Group.objects.create(name="A"), Group.objects.create(name="B")
        self.luisa.groups.add(g1, g2)
        self.dai(group=g1)
        self.dai(group=g2)
        self.dai(user=self.luisa)
        self.assertEqual(Document.objects.visible_to(self.luisa).count(), 1)

    def test_revoca_toglie_la_visibilita(self):
        p = self.dai(user=self.luisa)
        p.delete()
        self.assertFalse(Document.objects.visible_to(self.luisa).exists())


class ServiziCondivisioneTests(PermBase):
    def test_condividi_e_aggiorna(self):
        p, creata = share_document(user=self.mario, document=self.doc, level=L.READ, grantee_user=self.luisa)
        self.assertTrue(creata)
        p2, creata = share_document(user=self.mario, document=self.doc, level=L.WRITE, grantee_user=self.luisa)
        self.assertFalse(creata)
        self.assertEqual((p2.pk, p2.level, p2.granted_by), (p.pk, L.WRITE, self.mario))
        self.assertEqual(DocumentPermission.objects.count(), 1)

    def test_con_gruppo(self):
        g = Group.objects.create(name="G")
        p, _ = share_document(user=self.mario, document=self.doc, level=L.READ, grantee_group=g)
        self.assertEqual(p.group, g)

    def test_regole_di_dominio(self):
        with self.assertRaises(CondivisioneNonValida):  # al proprietario
            share_document(user=self.mario, document=self.doc, level=L.READ, grantee_user=self.mario)
        with self.assertRaises(CondivisioneNonValida):  # nessun destinatario
            share_document(user=self.mario, document=self.doc, level=L.READ)
        with self.assertRaises(CondivisioneNonValida):  # livello inventato
            share_document(user=self.mario, document=self.doc, level=99, grantee_user=self.luisa)
        self.luisa.is_active = False
        self.luisa.save()
        with self.assertRaises(CondivisioneNonValida):
            share_document(user=self.mario, document=self.doc, level=L.READ, grantee_user=self.luisa)

    def test_solo_admin_puo_condividere(self):
        self.dai(user=self.luisa, level=L.WRITE)
        with self.assertRaises(PermissionDenied):
            share_document(user=self.luisa, document=self.doc, level=L.READ, grantee_user=self.staff)
        with self.assertRaises(PermissionDenied):
            share_document(user=self.staff, document=self.doc, level=L.READ, grantee_user=self.staff)

    def test_un_admin_non_proprietario_puo_condividere(self):
        self.dai(user=self.luisa, level=L.ADMIN)
        share_document(user=self.luisa, document=self.doc, level=L.READ, grantee_user=self.staff)
        self.assertTrue(permissions.can_view(self.staff, self.doc))

    def test_revoca(self):
        p = self.dai(user=self.luisa)
        revoke_share(user=self.mario, document=self.doc, permission=p)
        self.assertFalse(DocumentPermission.objects.exists())

    def test_revoca_negata_e_condivisione_di_altro_documento(self):
        p = self.dai(user=self.luisa, level=L.WRITE)
        with self.assertRaises(PermissionDenied):
            revoke_share(user=self.luisa, document=self.doc, permission=p)
        altro = self.nuovo_documento(owner=self.mario, titolo="Altro", uploaded_file=self.file(contenuto=b"zz"))
        with self.assertRaises(PermissionDenied):
            revoke_share(user=self.mario, document=altro, permission=p)


class ViewPerLivelloTests(PermBase):
    """Cosa succede davvero nelle view a ogni livello: 404 senza accesso, 403 se l'azione eccede il livello."""

    def azioni(self):
        d = self.doc.pk
        return {
            "detail": ("get", reverse("documents:detail", args=[d])),
            "download": ("get", reverse("documents:download", args=[d])),
            "ver_download": ("get", reverse("documents:version_download", args=[d, 1])),
            "edit": ("get", reverse("documents:edit", args=[d])),
            "ver_new": ("get", reverse("documents:version_create", args=[d])),
            "restore": ("get", reverse("documents:version_restore", args=[d, 1])),
            "delete": ("get", reverse("documents:delete", args=[d])),
            "share": ("get", reverse("documents:share", args=[d])),
        }

    def codici(self, user):
        self.client.force_login(user)
        return {n: self.client.get(u).status_code for n, (_, u) in self.azioni().items()}

    def test_nessun_accesso_404_ovunque(self):
        self.assertEqual(set(self.codici(self.luisa).values()), {404})

    def test_lettura(self):
        self.dai(user=self.luisa, level=L.READ)
        c = self.codici(self.luisa)
        for n in ("detail", "download", "ver_download"):
            self.assertEqual(c[n], 200, n)
        for n in ("edit", "ver_new", "restore", "delete", "share"):
            self.assertEqual(c[n], 403, n)

    def test_modifica(self):
        self.dai(user=self.luisa, level=L.WRITE)
        c = self.codici(self.luisa)
        for n in ("detail", "download", "ver_download", "edit", "ver_new", "restore"):
            self.assertEqual(c[n], 200, n)
        for n in ("delete", "share"):
            self.assertEqual(c[n], 403, n)

    def test_amministrazione(self):
        self.dai(user=self.luisa, level=L.ADMIN)
        self.assertEqual(set(self.codici(self.luisa).values()), {200})

    def test_azioni_di_scrittura_bloccate_anche_in_post(self):
        self.dai(user=self.luisa, level=L.READ)
        self.client.force_login(self.luisa)
        d = self.doc.pk
        self.assertEqual(self.client.post(reverse("documents:edit", args=[d]), {"title": "hack"}).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("documents:version_create", args=[d]), {"file": self.file(contenuto=b"hack")}).status_code, 403
        )
        self.assertEqual(self.client.post(reverse("documents:delete", args=[d])).status_code, 403)
        self.assertEqual(self.client.delete(reverse("documents:delete", args=[d]), **HTMX).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("documents:share", args=[d]), {"destinatario": f"u:{self.staff.pk}", "livello": 10}).status_code, 403
        )
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.title, "Di Mario")
        self.assertEqual(self.doc.versions.count(), 1)
        self.assertEqual(DocumentPermission.objects.count(), 1)

    def test_scrittura_consentita_a_chi_ha_modifica(self):
        self.dai(user=self.luisa, level=L.WRITE)
        self.client.force_login(self.luisa)
        r = self.client.post(reverse("documents:edit", args=[self.doc.pk]), {"title": "Cambiato da Luisa", "description": ""})
        self.assertEqual(r.status_code, 302)
        r = self.client.post(
            reverse("documents:version_create", args=[self.doc.pk]), {"file": self.file(contenuto=b"nuova"), "comment": ""}
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.doc.versions.get(version_number=2).created_by, self.luisa)

    def test_delete_da_admin_non_proprietario(self):
        self.dai(user=self.luisa, level=L.ADMIN)
        self.client.force_login(self.luisa)
        self.assertEqual(self.client.post(reverse("documents:delete", args=[self.doc.pk])).status_code, 302)
        self.assertFalse(Document.objects.exists())

    def test_revocato_torna_404(self):
        p = self.dai(user=self.luisa, level=L.WRITE)
        p.delete()
        self.assertEqual(set(self.codici(self.luisa).values()), {404})

    def test_accesso_tramite_gruppo(self):
        g = Group.objects.create(name="Legale")
        self.luisa.groups.add(g)
        self.dai(group=g, level=L.READ)
        self.assertEqual(self.codici(self.luisa)["download"], 200)
        self.assertEqual(self.codici(self.staff)["download"], 404)


class InterfacciaTests(PermBase):
    def test_pulsanti_secondo_il_livello(self):
        self.dai(user=self.luisa, level=L.READ)
        self.client.force_login(self.luisa)
        r = self.client.get(self.doc.get_absolute_url())
        for nome in ("edit", "version_create", "delete", "share"):
            self.assertNotContains(r, reverse(f"documents:{nome}", args=[self.doc.pk]))
        self.assertContains(r, "Lettura")
        DocumentPermission.objects.update(level=L.ADMIN)
        r = self.client.get(self.doc.get_absolute_url())
        for nome in ("edit", "version_create", "delete", "share"):
            self.assertContains(r, reverse(f"documents:{nome}", args=[self.doc.pk]))

    def test_elenco_condivisi_con_me_e_filtri(self):
        mio = self.nuovo_documento(owner=self.luisa, titolo="Mio di Luisa", uploaded_file=self.file(contenuto=b"l"))
        self.dai(user=self.luisa, level=L.READ)
        self.client.force_login(self.luisa)
        r = self.client.get(reverse("documents:list"))
        self.assertContains(r, "Di Mario")
        self.assertContains(r, "Mio di Luisa")
        r = self.client.get(reverse("documents:list"), {"scope": "miei"})
        self.assertContains(r, "Mio di Luisa")
        self.assertNotContains(r, "Di Mario")
        r = self.client.get(reverse("documents:list"), {"scope": "condivisi"})
        self.assertContains(r, "Di Mario")
        self.assertNotContains(r, "Mio di Luisa")

    def test_elimina_nell_elenco_solo_se_admin(self):
        self.dai(user=self.luisa, level=L.WRITE)
        self.client.force_login(self.luisa)
        self.assertNotContains(self.client.get(reverse("documents:list")), "hx-delete")
        DocumentPermission.objects.update(level=L.ADMIN)
        self.assertContains(self.client.get(reverse("documents:list")), "hx-delete")

    def test_la_ricerca_trova_solo_cio_che_e_condiviso(self):
        self.client.force_login(self.luisa)
        self.assertNotContains(self.client.get(reverse("documents:list"), {"q": "mario"}), "Di Mario")
        self.dai(user=self.luisa)
        self.assertContains(self.client.get(reverse("documents:list"), {"q": "mario"}), "Di Mario")

    def test_dashboard_conta_anche_i_condivisi(self):
        self.dai(user=self.luisa)
        self.client.force_login(self.luisa)
        self.assertContains(self.client.get(reverse("core:dashboard")), "1 documento")


class PaginaCondivisioneTests(PermBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.mario)
        self.url = reverse("documents:share", args=[self.doc.pk])

    def test_pagina_non_elenca_il_proprietario_tra_i_destinatari(self):
        r = self.client.get(self.url)
        self.assertContains(r, "luisa")
        self.assertNotContains(r, 'value="u:%d"' % self.mario.pk)

    def test_condividi_con_htmx(self):
        r = self.client.post(self.url, {"destinatario": f"u:{self.luisa.pk}", "livello": L.WRITE}, **HTMX)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "<html")
        self.assertContains(r, "Modifica")
        p = DocumentPermission.objects.get()
        self.assertEqual((p.user, p.level, p.granted_by), (self.luisa, L.WRITE, self.mario))

    def test_condividi_con_gruppo_senza_htmx(self):
        g = Group.objects.create(name="Legale")
        r = self.client.post(self.url, {"destinatario": f"g:{g.pk}", "livello": L.READ})
        self.assertRedirects(r, self.url)
        self.assertEqual(DocumentPermission.objects.get().group, g)

    def test_input_non_validi_422(self):
        for dati in (
            {"destinatario": "", "livello": L.READ},
            {"destinatario": "u:99999", "livello": L.READ},
            {"destinatario": "x:1", "livello": L.READ},
            {"destinatario": f"u:{self.luisa.pk}", "livello": 99},
            {"destinatario": f"u:{self.mario.pk}", "livello": L.READ},  # il proprietario
            {},
        ):
            with self.subTest(dati=dati):
                self.assertEqual(self.client.post(self.url, dati, **HTMX).status_code, 422)
        self.assertEqual(DocumentPermission.objects.count(), 0)

    def test_revoca_htmx(self):
        p = self.dai(user=self.luisa)
        r = self.client.post(reverse("documents:share_revoke", args=[self.doc.pk, p.pk]), **HTMX)
        self.assertEqual((r.status_code, r.content), (200, b""))
        self.assertEqual(DocumentPermission.objects.count(), 0)

    def test_revoca_a_se_stessi_rimanda_all_elenco(self):
        p = self.dai(user=self.luisa, level=L.ADMIN)
        self.client.force_login(self.luisa)
        r = self.client.post(reverse("documents:share_revoke", args=[self.doc.pk, p.pk]), **HTMX)
        self.assertEqual(r["HX-Redirect"], reverse("documents:list"))
        r = self.client.post(reverse("documents:share_revoke", args=[self.doc.pk, self.dai(user=self.staff).pk]))
        self.assertEqual(r.status_code, 404)  # ormai non vede più il documento

    def test_revoca_con_id_di_altro_documento_da_404(self):
        altro = self.nuovo_documento(owner=self.mario, titolo="Altro", uploaded_file=self.file(contenuto=b"q"))
        p = DocumentPermission.objects.create(document=altro, user=self.luisa, level=L.READ)
        r = self.client.post(reverse("documents:share_revoke", args=[self.doc.pk, p.pk]))
        self.assertEqual(r.status_code, 404)
        self.assertTrue(DocumentPermission.objects.filter(pk=p.pk).exists())

    def test_anonimo_al_login(self):
        self.client.logout()
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r["Location"])

    def test_utenti_inattivi_non_sono_proposti(self):
        self.luisa.is_active = False
        self.luisa.save()
        self.assertNotContains(self.client.get(self.url), f'value="u:{self.luisa.pk}"')
