#!/usr/bin/env python3
"""Create or update a login for the Bank Statement Analyser.

Passwords are stored only as a PBKDF2-HMAC-SHA256 hash with a per-user random
salt — never in plain text, and never recoverable. To change a password, set a
new one; there is nothing to read back.

    python scripts/manage_users.py add admin@getitright.co.in --role admin
    python scripts/manage_users.py add ops@getitright.co.in   --role customer
    python scripts/manage_users.py list
    python scripts/manage_users.py remove ops@getitright.co.in

The table name comes from the stack output AuthTableName, or --table.
"""
import argparse
import getpass
import hashlib
import os
import secrets
import sys

import boto3

PBKDF2_ROUNDS = 210_000          # must match backend/api/handler.py


def table(name, region):
    if not name:
        cf = boto3.client("cloudformation", region_name=region)
        outs = cf.describe_stacks(StackName="BsaStack")["Stacks"][0]["Outputs"]
        name = next(o["OutputValue"] for o in outs
                    if o["OutputKey"] == "AuthTableName")
    return boto3.resource("dynamodb", region_name=region).Table(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["add", "list", "remove"])
    ap.add_argument("email", nargs="?")
    ap.add_argument("--role", default="customer", choices=["admin", "customer"])
    ap.add_argument("--password")
    ap.add_argument("--table")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-south-1"))
    a = ap.parse_args()
    t = table(a.table, a.region)

    if a.action == "list":
        for it in t.scan().get("Items", []):
            if str(it.get("pk", "")).startswith("USER#"):
                print(f"  {it['pk'][5:]:38} {it.get('role')}")
        return

    if not a.email:
        sys.exit("email required")
    email = a.email.strip().lower()

    if a.action == "remove":
        t.delete_item(Key={"pk": f"USER#{email}"})
        print(f"removed {email}")
        return

    pw = a.password or getpass.getpass(f"password for {email}: ")
    if len(pw) < 10:
        sys.exit("password must be at least 10 characters")
    salt = secrets.token_bytes(16).hex()
    t.put_item(Item={
        "pk": f"USER#{email}", "email": email, "role": a.role, "salt": salt,
        "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(),
                                    bytes.fromhex(salt), PBKDF2_ROUNDS).hex(),
        "rounds": PBKDF2_ROUNDS,
    })
    print(f"{email} saved with role {a.role}")


if __name__ == "__main__":
    main()
