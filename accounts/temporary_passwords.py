"""Temporary passwords, for users who have none (``issue_temporary_passwords``).

JI sends no email, so there is no "forgot password" link to send. A user with
no usable password — every SAP Portal user ``import_portal_users`` created,
since portal password hashes are never carried over — gets one this way: an
administrator issues a temporary password, hands it over, and the app makes the
user choose their own at first login (``User.must_change_password``, cleared by
the change-password endpoint).

A temporary password is 12 characters from an alphabet without look-alikes
(no 0/O, 1/l/I), readable over a phone call.
"""

import secrets

from django.db import transaction

# Lower and upper case and digits, minus the characters people misread.
_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LENGTH = 12


def new_temporary_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(LENGTH))


@transaction.atomic
def issue(users) -> list[tuple]:
    """Give each user a fresh temporary password; ``[(user, password), ...]``.

    The password is returned once and never stored in clear; the caller hands
    it over. Each user must change it at their next login.
    """
    issued = []
    for user in users:
        password = new_temporary_password()
        user.set_password(password)
        user.must_change_password = True
        user.save(update_fields=["password", "must_change_password"])
        issued.append((user, password))
    return issued
