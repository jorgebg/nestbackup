import os
import socket
import subprocess
import time
from datetime import datetime

import boto3
import requests
from botocore.client import Config
from dateutil.tz import tzlocal
from peewee import (CharField, DatabaseProxy, DateField, Model,
                    PostgresqlDatabase)
from playhouse.mysql_ext import MySQLConnectorDatabase

WAIT_SERVICE_TIMEOUT = 10.0

BACKUP_RETENTION = 2
BACKUP_RUNS = 3  # retention is set to 2 in the config, so only 2 copies should remain

s3 = None
db_proxy = DatabaseProxy()
expected_rows = {}
first_backup_run_date = None


class Person(Model):
    name = CharField()
    birthday = DateField()

    class Meta:
        database = db_proxy


def db_iter():
    for db_adapter, db_host in (
        (MySQLConnectorDatabase, "mysql"),
        (PostgresqlDatabase, "postgres"),
    ):
        db = db_adapter("app", user="app", password="app", host=db_host)
        # db.url = get_db_url(db, scheme=db_host)
        db_proxy.initialize(db)
        yield db


def run(cmd):
    try:
        result = subprocess.check_output(cmd, shell=True, universal_newlines=True)
        print(result)
        return result
    except subprocess.CalledProcessError as e:
        print(e.stdout, e.stderr)
        raise e


def step(fun):
    def wrapper():
        print(fun.__name__.replace("_", " ").title() + "...", end="")
        fun()
        print("Done")

    return wrapper


@step
def wait_for_services():
    for host, port in (
        ("mysql", 3306),
        ("postgres", 5432),
        ("minio", 9000),
        ("mailhog", 1025),
    ):
        start_time = time.perf_counter()
        while True:
            try:
                with socket.create_connection(
                    (host, port), timeout=WAIT_SERVICE_TIMEOUT
                ):
                    break
            except OSError as ex:
                time.sleep(0.01)
                if time.perf_counter() - start_time >= WAIT_SERVICE_TIMEOUT:
                    raise TimeoutError(
                        "Waited too long for the port {} on host {} to start accepting "
                        "connections.".format(port, host)
                    ) from ex


@step
def setup_s3():
    global s3
    s3 = boto3.client(
        "s3",
        endpoint_url="http://minio:9000",
        aws_access_key_id="app",
        aws_secret_access_key="12345678",
        config=Config(signature_version="s3v4"),
    )
    s3.create_bucket(Bucket="backup")
    run("aws configure set aws_access_key_id app")
    run("aws configure set aws_secret_access_key 12345678")


@step
def setup_databases():
    for db in db_iter():
        db.connect()
        db.create_tables((Person,))
        Person.create(name="Mochuelo", birthday=datetime(2000, 1, 1))
        expected_rows[db.connect_params["host"]] = list(Person.select())
        db.close()


@step
def setup_sync_data():
    run("mkdir -p /tmp/sync/foo")
    run('echo "I am foo/bar.txt" > /tmp/sync/foo/bar.txt')


@step
def backup():
    global first_backup_run_date
    for i in range(BACKUP_RUNS):
        run("NESTBACKUP_CONFIG=/app/backup.ini nestbackup backup -vv")
        if first_backup_run_date is None:
            first_backup_run_date = datetime.now(tz=tzlocal())


@step
def prune_databases():
    for db in db_iter():
        db.connect()
        db.drop_tables((Person,))
        assert [] == db.get_tables()
        db.close()


@step
def rename_sync_data():
    run("mv /tmp/sync /tmp/sync-original")


@step
def restore():
    run("NESTBACKUP_CONFIG=/app/backup.ini nestbackup -f restore")


@step
def check_databases():
    for prefix in ("/server/postgres/postgresql_", "/server/mysql/mysql_"):
        response = s3.list_objects_v2(Bucket="backup", Prefix=prefix)
        datetime_list = [item["LastModified"] for item in response["Contents"]]
        # retention is set to 2 in the config, so only the last 2 copies should remain
        assert len(datetime_list) == 2
        for d in datetime_list:
            assert d > first_backup_run_date

    for db in db_iter():
        db.connect()
        assert expected_rows[db.connect_params["host"]] == list(Person.select())
        db.close()


@step
def check_sync_data():
    run("diff /tmp/sync /tmp/sync-original")


@step
def check_notify():
    r = requests.get("http://mailhog:8025/api/v2/messages")
    assert r.json()["count"] == BACKUP_RUNS
    msg = r.json()["items"][0]["Raw"]
    expected_msg_keys = {
        "From": "test@server",
        "To": ["admin@mailhog.com"],
        "Helo": "[{}]".format(socket.gethostbyname(socket.gethostname())),
    }
    for k, v in expected_msg_keys.items():
        assert msg[k] == v, "{} should be {}".format(k, v)

    expected_reports = [
        "Subject: Backup report for server: Success",
        "upload: s3://backup/server/postgres/postgresql_",
        "delete: s3://backup/server/postgres/postgresql_",
        "upload: s3://backup/server/mysql/mysql_",
        "delete: s3://backup/server/mysql/mysql_",
    ]
    for line in expected_reports:
        assert line in msg["Data"], 'Data should contain "{}"'.format(line)


if __name__ == "__main__":
    wait_for_services()
    setup_s3()
    setup_databases()
    setup_sync_data()
    backup()
    prune_databases()
    rename_sync_data()
    restore()
    check_databases()
    check_sync_data()
    check_notify()
