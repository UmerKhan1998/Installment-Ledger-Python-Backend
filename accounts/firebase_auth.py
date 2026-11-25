"""Firebase Auth for the API: the web/mobile app signs in with Firebase and sends its ID token as a Bearer token.

Verification needs only the project id and Google's public certificates (no service account):
signature (RS256, Google's rotating certs, cached per their max-age), audience = project id,
issuer = https://securetoken.google.com/<project id>, not expired, non-empty subject (uid).

Linking to Django users (so existing data carries over):
1. a known uid -> its user;
2. else a *verified* email that matches an existing account -> link it;
3. else create a new user.
Unverified emails are refused, so nobody can take over an account by signing up with someone else's address.

Django's own JWT login keeps working next to this (tokens that aren't Firebase tokens fall through to it).
"""
import json
import re
import urllib.request

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from google.auth import jwt as google_jwt
from rest_framework import authentication, exceptions

from .models import FirebaseIdentity

CERTS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
CERTS_CACHE_KEY = "firebase:securetoken:certs"


def _google_certs():
    certs = cache.get(CERTS_CACHE_KEY)
    if certs:
        return certs
    req = urllib.request.Request(CERTS_URL, headers={"User-Agent": "installment-ledger/1.0"})
    with urllib.request.urlopen(req, timeout=6) as res:
        certs = json.load(res)
        match = re.search(r"max-age=(\d+)", res.headers.get("Cache-Control", ""))
    cache.set(CERTS_CACHE_KEY, certs, min(int(match.group(1)) if match else 3600, 6 * 3600))
    return certs


def verify_firebase_token(token):
    """Verified claims of a Firebase ID token, or raise AuthenticationFailed."""
    project = settings.FIREBASE_PROJECT_ID
    if not project:
        raise exceptions.AuthenticationFailed("Firebase sign-in is not configured on the server.")
    try:
        claims = google_jwt.decode(token, certs=_google_certs(), audience=project, clock_skew_in_seconds=60)
    except exceptions.APIException:
        raise
    except Exception:
        raise exceptions.AuthenticationFailed("Your sign-in has expired or is invalid. Please sign in again.")
    if claims.get("iss") != f"https://securetoken.google.com/{project}" or not claims.get("sub"):
        raise exceptions.AuthenticationFailed("This sign-in token is not for this app.")
    return claims


def user_for_claims(claims):
    """The Django user for verified Firebase claims (linking or creating as described above)."""
    User = get_user_model()
    uid = claims["sub"]
    provider = (claims.get("firebase") or {}).get("sign_in_provider", "")
    email = (claims.get("email") or "").strip().lower()

    identity = FirebaseIdentity.objects.select_related("user").filter(uid=uid).first()
    if identity:
        if not identity.user.is_active:
            raise exceptions.AuthenticationFailed("This account is disabled.")
        FirebaseIdentity.objects.filter(pk=identity.pk).update(email=email or identity.email, sign_in_provider=provider)
        return identity.user

    if not email or not claims.get("email_verified"):
        raise exceptions.AuthenticationFailed("Verify your email address first (check your inbox), then sign in again.")

    name = (claims.get("name") or "").strip()
    first, _, last = name.partition(" ")
    try:
        with transaction.atomic():
            user = User.objects.filter(username=email).first() or User.objects.filter(email__iexact=email).first()
            if user is None:
                user = User.objects.create_user(username=email, email=email, first_name=first[:150], last_name=last[:150])
                user.set_unusable_password()
                user.save(update_fields=["password"])
            elif hasattr(user, "firebase"):
                raise exceptions.AuthenticationFailed("This email is already linked to another sign-in. Use that one.")
            FirebaseIdentity.objects.create(user=user, uid=uid, email=email, sign_in_provider=provider)
    except IntegrityError:
        # Another request linked this uid at the same moment.
        return FirebaseIdentity.objects.select_related("user").get(uid=uid).user
    return user


class FirebaseAuthentication(authentication.BaseAuthentication):
    """`Authorization: Bearer <Firebase ID token>`. Other Bearer tokens (Django JWTs) are left to the next class."""

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode("latin-1")
        if not header.lower().startswith("bearer "):
            return None
        token = header[7:].strip()
        try:
            unverified = google_jwt.decode_header(token) if hasattr(google_jwt, "decode_header") else _header(token)
        except Exception:
            return None
        if unverified.get("alg") != "RS256" or "kid" not in unverified:
            return None  # a Django (HS256) token: let JWTAuthentication handle it
        claims = verify_firebase_token(token)
        return user_for_claims(claims), claims

    def authenticate_header(self, request):
        return 'Bearer realm="api"'


def _header(token):
    import base64

    segment = token.split(".")[0]
    segment += "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment))
