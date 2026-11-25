"""Account-binding safety: once a Django account is linked to a Firebase sign-in, Firebase is its only way in.

Why: Django registration never verified email addresses. Someone could register a Django account with another
person's email, wait for the real owner to sign in with Firebase (which links by verified email), and keep
using the Django password or tokens to read the owner's data. Linking therefore
  * removes the Django password, and
  * blacklists every outstanding Django refresh token,
and LegacyJWTAuthentication rejects Django access tokens of linked accounts (they live up to 15 minutes).
"""
from django.contrib.auth.hashers import make_password
from rest_framework import exceptions
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

FIREBASE_ONLY = "This account signs in with Google / Firebase. Use “Continue with Google” or your Firebase email sign-in."


def lock_to_firebase(user):
    """Turn off Django password login and revoke all Django refresh tokens for `user`."""
    if user.has_usable_password():
        user.password = make_password(None)  # unusable
        user.save(update_fields=["password"])
    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


def is_firebase_linked(user):
    from .models import FirebaseIdentity

    return FirebaseIdentity.objects.filter(user=user).exists()


class LegacyJWTAuthentication(JWTAuthentication):
    """Django JWTs, but never for accounts that are linked to Firebase."""

    def authenticate(self, request):
        result = super().authenticate(request)
        if result and is_firebase_linked(result[0]):
            raise exceptions.AuthenticationFailed(FIREBASE_ONLY)
        return result
