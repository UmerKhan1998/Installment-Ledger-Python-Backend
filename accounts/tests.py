import datetime
import time
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.contrib.auth import get_user_model
from django.test import override_settings
from google.auth import crypt
from google.auth import jwt as google_jwt
from rest_framework.test import APITestCase

from .models import FirebaseIdentity

PROJECT = "elavora-test"


def make_signer():
    """An RSA key + self-signed cert, standing in for Google's securetoken signing keys."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "securetoken-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return crypt.RSASigner.from_string(pem_key, key_id="kid-1"), {"kid-1": cert.public_bytes(serialization.Encoding.PEM).decode()}


SIGNER, CERTS = make_signer()


def firebase_token(uid="uid-1", email="me@example.com", verified=True, aud=PROJECT, exp_in=3600, name="Umer Khan", provider="password"):
    now = int(time.time())
    claims = {
        "iss": f"https://securetoken.google.com/{aud}", "aud": aud, "sub": uid, "user_id": uid,
        "iat": now, "exp": now + exp_in, "auth_time": now, "email": email, "email_verified": verified, "name": name,
        "firebase": {"sign_in_provider": provider, "identities": {}},
    }
    return google_jwt.encode(SIGNER, claims, header={"kid": "kid-1"}).decode()


@override_settings(FIREBASE_PROJECT_ID=PROJECT)
class FirebaseAuthTests(APITestCase):
    def setUp(self):
        patcher = mock.patch("accounts.firebase_auth._google_certs", return_value=CERTS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def me(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return self.client.get("/api/auth/me/")

    def test_new_verified_user_is_created(self):
        res = self.me(firebase_token())
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data["email"], res.data["first_name"], res.data["last_name"]), ("me@example.com", "Umer", "Khan"))
        user = get_user_model().objects.get(username="me@example.com")
        self.assertFalse(user.has_usable_password())  # Firebase holds the password
        self.assertEqual(user.firebase.uid, "uid-1")
        self.assertEqual(self.me(firebase_token()).data["id"], user.id)  # same uid -> same user
        self.assertEqual(FirebaseIdentity.objects.count(), 1)

    def test_existing_account_is_linked_by_verified_email_and_keeps_its_data(self):
        reg = self.client.post("/api/auth/register/", {"email": "old@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {reg.data['access']}")
        self.client.post("/api/plans/", {"item": "MacBook", "purchase_price": "1199", "total_amount": "1350", "months": 18,
                                         "first_payment_date": "2026-11-01"}, format="json")
        res = self.me(firebase_token(uid="g-123", email="Old@Example.com", provider="google.com"))
        self.assertEqual(res.data["id"], reg.data["user"]["id"])
        self.assertEqual(len(self.client.get("/api/plans/").data), 1)
        self.assertEqual(FirebaseIdentity.objects.get().sign_in_provider, "google.com")

    def test_unverified_email_is_refused(self):
        res = self.me(firebase_token(verified=False))
        self.assertEqual(res.status_code, 401)
        self.assertIn("Verify your email", res.data["detail"])
        self.assertFalse(get_user_model().objects.exists())

    def test_wrong_project_expired_or_tampered_tokens_are_refused(self):
        self.assertEqual(self.me(firebase_token(aud="someone-else")).status_code, 401)
        self.assertEqual(self.me(firebase_token(exp_in=-3600)).status_code, 401)
        token = firebase_token()
        head, body, sig = token.split(".")
        self.assertEqual(self.me(f"{head}.{body}.{sig[:-4]}AAAA").status_code, 401)

    def test_django_jwt_login_still_works(self):
        reg = self.client.post("/api/auth/register/", {"email": "jwt@example.com", "password": "Sup3r-secret!"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {reg.data['access']}")
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 200)

    def test_one_email_cannot_be_linked_to_two_firebase_accounts(self):
        self.me(firebase_token(uid="uid-a"))
        res = self.me(firebase_token(uid="uid-b"))
        self.assertEqual(res.status_code, 401)
        self.assertIn("already linked", res.data["detail"])

    @override_settings(FIREBASE_PROJECT_ID="")
    def test_not_configured(self):
        self.assertEqual(self.me(firebase_token()).status_code, 401)


@override_settings(FIREBASE_PROJECT_ID=PROJECT)
class AccountBindingTests(APITestCase):
    """Once an account is linked to Firebase, only Firebase opens it (no leftover Django password or tokens)."""

    def setUp(self):
        patcher = mock.patch("accounts.firebase_auth._google_certs", return_value=CERTS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_squatter_loses_access_when_real_owner_signs_in(self):
        # Someone registers a Django account with the victim's email (Django never verified emails)...
        squat = self.client.post("/api/auth/register/", {"email": "victim@example.com", "password": "Squatter-pass1"}, format="json").data
        # ...the real owner signs in with Google (verified email) and the account gets linked.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {firebase_token(uid='victim-g', email='victim@example.com', provider='google.com')}")
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 200)
        self.client.post("/api/plans/", {"item": "Victim's private plan", "purchase_price": "1", "total_amount": "10", "months": 12,
                                         "first_payment_date": "2026-11-01"}, format="json")
        # The squatter's password, access token and refresh token are all dead now.
        self.client.credentials()
        login = self.client.post("/api/auth/login/", {"email": "victim@example.com", "password": "Squatter-pass1"}, format="json")
        self.assertEqual(login.status_code, 400)
        self.assertIn("Google / Firebase", str(login.data))
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {squat['access']}")
        self.assertEqual(self.client.get("/api/plans/").status_code, 401)
        self.client.credentials()
        self.assertEqual(self.client.post("/api/auth/refresh/", {"refresh": squat["refresh"]}, format="json").status_code, 401)
        self.assertFalse(get_user_model().objects.get(username="victim@example.com").has_usable_password())

    def test_unlinked_accounts_keep_django_login(self):
        self.client.post("/api/auth/register/", {"email": "plain@example.com", "password": "Plain-pass1!"}, format="json")
        res = self.client.post("/api/auth/login/", {"email": "plain@example.com", "password": "Plain-pass1!"}, format="json")
        self.assertEqual(res.status_code, 200)

    def test_two_firebase_users_never_see_each_other(self):
        alice = firebase_token(uid="alice-uid", email="alice@example.com")
        bob = firebase_token(uid="bob-uid", email="bob@example.com")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {alice}")
        plan = self.client.post("/api/plans/", {"item": "Alice plan", "purchase_price": "1", "total_amount": "10", "months": 12,
                                                "first_payment_date": "2026-11-01"}, format="json").data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {bob}")
        self.assertEqual(self.client.get("/api/plans/").data, [])
        self.assertEqual(self.client.get(f"/api/plans/{plan['id']}/").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/plans/{plan['id']}/").status_code, 404)
        self.assertNotEqual(self.client.get("/api/auth/me/").data["email"], "alice@example.com")
