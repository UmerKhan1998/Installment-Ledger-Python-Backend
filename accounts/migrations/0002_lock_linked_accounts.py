from django.contrib.auth.hashers import make_password
from django.db import migrations


def lock_linked(apps, schema_editor):
    """Accounts already linked to Firebase: remove their Django password and revoke Django refresh tokens."""
    FirebaseIdentity = apps.get_model("accounts", "FirebaseIdentity")
    User = apps.get_model("auth", "User")
    OutstandingToken = apps.get_model("token_blacklist", "OutstandingToken")
    BlacklistedToken = apps.get_model("token_blacklist", "BlacklistedToken")
    user_ids = list(FirebaseIdentity.objects.values_list("user_id", flat=True))
    for user in User.objects.filter(id__in=user_ids):
        user.password = make_password(None)
        user.save(update_fields=["password"])
    for token in OutstandingToken.objects.filter(user_id__in=user_ids):
        BlacklistedToken.objects.get_or_create(token=token)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_firebase_identity"),
        ("token_blacklist", "0012_alter_outstandingtoken_user"),
    ]
    operations = [migrations.RunPython(lock_linked, migrations.RunPython.noop)]
