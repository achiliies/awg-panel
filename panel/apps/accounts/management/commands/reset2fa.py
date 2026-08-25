"""Remove a user's TOTP devices from the server shell.

The way out of a lost or wiped authenticator. It is deliberately not reachable
over HTTP: anyone who can run it already has root on the box, and an endpoint
that switches the second factor off would be a way around it.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django_otp.plugins.otp_totp.models import TOTPDevice


class Command(BaseCommand):
    help = "Remove every TOTP device for a user, turning two-factor sign-in off for them."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "username",
            nargs="?",
            default="",
            help="Account to clear. Optional when the panel has a single account.",
        )

    def handle(self, *args: object, **options: object) -> None:
        user = self._resolve_user(str(options["username"]).strip())

        devices = TOTPDevice.objects.filter(user=user)
        count = devices.count()
        if not count:
            self.stdout.write(f"{user.get_username()} has no TOTP device; nothing to do.")
            return

        devices.delete()
        noun = "device" if count == 1 else "devices"
        self.stdout.write(
            f"Removed {count} TOTP {noun} for {user.get_username()}. "
            "Two-factor sign-in is off for that account; set it up again from Settings."
        )

    def _resolve_user(self, username: str):
        user_model = get_user_model()
        field = user_model.USERNAME_FIELD

        if not username:
            # The panel is installed with exactly one account, and the operator
            # running this has usually just been locked out of it.
            users = list(user_model.objects.all()[:2])
            if len(users) == 1:
                return users[0]
            raise CommandError("Name the account to clear, for example: awg-panel reset-2fa admin")

        try:
            return user_model.objects.get(**{field: username})
        except user_model.DoesNotExist:
            known = ", ".join(sorted(user_model.objects.values_list(field, flat=True)[:20]))
            raise CommandError(
                f"There is no account named '{username}'."
                + (f" Known accounts: {known}." if known else " The panel has no accounts yet.")
            ) from None
