# -*- coding: utf-8 -*-
# Python 2 assumes ASCII source without this line, and the prose below
# uses em dashes. PEP 263, and a reminder of which interpreter this is.
#
# Seed the demo bank.
#
# Through the ORM rather than raw SQL, and through the same shape
# UserFormView uses when somebody registers, so the demo data is created the way
# a visitor's registration would create it. If that logic is wrong, the seed is
# wrong in the same way, which is the point.
#
# A STANDALONE script, run as `python seed.py`, not `manage.py shell < seed.py`.
# Django 1.9's shell reads piped input through the interactive console, which
# echoes each line back and chokes on a multi-line def — the first attempt at
# this printed the source of a print statement instead of running it, and seeded
# nothing while exiting 0.

import os

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings_shim')
django.setup()

from django.contrib.auth.models import User          # noqa: E402

# `from main import account_type`, not `import account_type`. main/views.py gets
# away with the bare form because Python 2 resolves an implicit relative import
# from inside the package that contains it — the single reason this whole image
# is python:2.7. This file sits outside main/, so it has to say where to look.
from main import account_type                        # noqa: E402
from main.models import Account                      # noqa: E402


def env(name, default=''):
    return os.environ.get(name, default)


def make_user(username, password, checking, savings):
    if User.objects.filter(username=username).exists():
        return
    user = User.objects.create_user(
        username=username,
        password=password,
        email='%s@example.invalid' % username,
    )

    # UserFormView creates exactly these two on registration: a savings account
    # with no card, and a checking account with a generated card number. A
    # credit account appears later, if and when you open a credit card from the
    # dashboard.
    Account.objects.create(
        user=user,
        card_num='',
        balance=savings,
        account_type=account_type.savings,
        card_activated=False,
        card_pin='',
    )

    checking_account = Account(
        user=user,
        balance=checking,
        account_type=account_type.checking,
        card_activated=True,
        # Four digits, in clear text, because that is what the model declares:
        # CharField(max_length=4). See site/pybank.html.
        card_pin='1234',
    )
    checking_account.card_num = checking_account.generate_card()
    checking_account.save()


def run():
    demo = env('PYBANK_DEMO_USER', 'demo')
    other = env('PYBANK_OTHER_USER', 'alex')
    checking = int(env('PYBANK_CHECKING_BALANCE', '4200'))
    savings = int(env('PYBANK_SAVINGS_BALANCE', '15000'))

    make_user(demo, env('PYBANK_DEMO_PASSWORD', 'seminoles'), checking, savings)
    # The counterparty, so "transfer to another user" is demoable without
    # registering a second account first. Deliberately poorer, so it is obvious
    # which direction the money went.
    make_user(other, env('PYBANK_OTHER_PASSWORD', 'seminoles'), 300, 900)

    users = User.objects.count()
    accounts = Account.objects.count()
    print('seeded: users=%d accounts=%d' % (users, accounts))
    # A seed that quietly does nothing is worse than one that fails, and the
    # first version of this script did exactly that.
    assert users >= 2 and accounts >= 4, 'seed produced nothing'


if __name__ == '__main__':
    run()
