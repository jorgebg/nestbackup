#!/usr/bin/env python3
"""
NestBackup

$ nestbackup --help
$ nestbackup init  # creates a base ~/backup.ini config file
$ nestbackup backup
$ nestbackup restore
$ nestbackup validate # validates the config
"""
import argparse
import configparser
import json
import logging
import os
import re
import smtplib
import socket
import ssl
import subprocess
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse

CONFIG_EXAMPLE_TEMPLATE = """\
[DEFAULT]
# The DEFAULT section contains the default values set for all the jobs
aws_access_key_id=app
aws_secret_access_key=12345678
bucket=backup

[media]
job=sync
local_path=/var/www

[db]
job=database
db_uri=postgresql://app:app@postgres/app
# db_uri=mysql://app:app@mysql/app
# keep 7 files, delete older ones
retention=7

[notify]
job=smtp
server=smtp.example.com
ssl=yes
port=465
username=test@example.com
password=test
recipients=admin@example.com
"""


logger = logging.getLogger("nestbackup")

ACTION_INIT = "init"
ACTION_BACKUP = "backup"
ACTION_RESTORE = "restore"
ACTION_VALIDATE = "validate"
ACTION_CHOICES = (ACTION_INIT, ACTION_BACKUP, ACTION_RESTORE, ACTION_VALIDATE)


def execute(cmd, **kwargs):
    popen = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, universal_newlines=True, shell=True, **kwargs
    )
    for stdout_line in iter(popen.stdout.readline, ""):
        yield stdout_line
    popen.stdout.close()
    return_code = popen.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, cmd)


class InvalidConfig(Exception):
    def __init__(self, config):
        super().__init__("Invalid config: {}".format(config))


class BackupNotFound(Exception):
    def __init__(self):
        super().__init__("Backup not found")


class Context(dict):
    def __getattr__(self, name):
        return self[name]

    def __setattr__(self, name, value):
        self[name] = value

    def __delattr__(self, name):
        del self[name]


class Report:
    def __init__(self):
        self._report_list = OrderedDict()
        self.error = False

    def add(self, section, lines):
        if section not in self._report_list:
            self._report_list[section] = []
        self._report_list[section] += lines

    def items(self):
        return self._report_list.items()


class JobManager:
    JOB_CLASS_MAP = {}

    @classmethod
    def register(cls, job_id):
        def registerer(job_cls):
            cls.JOB_CLASS_MAP[job_id] = job_cls
            return job_cls

        return registerer

    @classmethod
    def get(cls, job_id):
        return cls.JOB_CLASS_MAP[job_id]


class BaseJob:

    base_fields = (
        "name",
        "job",
        "endpoint_url",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_shared_credentials_file",
        "bucket",
        "hostname",
        "remote_path",
        "s3_bucket_url",
        "aws_cli",
    )

    fields = tuple()

    def __init__(self, section):
        self.context = ctx = Context()
        all_fields = self.base_fields + self.fields
        invalid_fields = set(section.keys()).difference(set(all_fields))
        if invalid_fields:
            raise InvalidConfig(invalid_fields)
        for field in all_fields:
            value = section.get(field)
            if field == "name" and value is None:
                value = section.name
            if field == "hostname" and value is None:
                value = socket.gethostname()
            elif field == "aws_cli" and value is None:
                value = "aws"
                if ctx.endpoint_url:
                    value += " --endpoint-url {endpoint_url}"
                value = value.format(**ctx)
            setattr(ctx, field, value)

        ctx.s3_path = "/".join(
            [ctx[k] for k in ("hostname", "name", "remote_path") if ctx[k] is not None]
        )
        ctx.s3_bucket_url = "s3://{bucket}/{s3_path}".format(**ctx)

        ctx.env = os.environ.copy()
        ctx.env.update(
            {
                "AWS_ACCESS_KEY_ID": ctx.aws_access_key_id,
                "AWS_SECRET_ACCESS_KEY": ctx.aws_secret_access_key,
            }
        )

    def run(self, command):
        output = ""
        for line in self.run_stream(command):
            output += line
        return output

    def run_stream(self, command):
        logger.debug("Run template: {}".format(command))
        command = command.format(**self.context)
        logger.info("Run: {}".format(command))
        for line in execute(command, env=self.context.env):
            logger.info("Output: " + line)
            yield line

    def backup(self):
        raise NotImplemented()

    def restore(self):
        raise NotImplemented()


@JobManager.register("sync")
class SyncJob(BaseJob):
    fields = ("local_path", "aws_extra_args")

    def __init__(self, section):
        super().__init__(section)
        ctx = self.context
        if ctx.aws_extra_args is None:
            ctx.aws_extra_args = ''

    def backup(self, report):
        ctx = self.context
        agg_operations = defaultdict(int)
        for line in self.run_stream("{aws_cli} s3 sync {aws_extra_args} {local_path} {s3_bucket_url}"):
            if re.match(r"\w+:.*", line):
                op = line.split(":")[0]
                agg_operations[op] += 1
        report.add(
            ctx.name,
            [
                "{}: {}".format(op, agg_operations[op])
                for op in sorted(agg_operations.keys())
            ]
            or ["No files out of sync"],
        )

    def restore(self):
        self.run("{aws_cli} s3 sync {s3_bucket_url} {local_path}")


@JobManager.register("database")
class DatabaseJob(BaseJob):
    fields = (
        "db_uri",
        "retention",
        "scheme",
        "hostname",
        "username",
        "password",
        "dbname",
        "su_user",
        "dump_filename",
    )

    def __init__(self, section):
        super().__init__(section)
        ctx = self.context
        if ctx.db_uri is not None:
            dbc = urlparse(ctx.db_uri)
            ctx.scheme = dbc.scheme
            ctx.hostname = dbc.hostname
            ctx.username = dbc.username
            ctx.password = dbc.password
            ctx.dbname = dbc.path.lstrip("/")
        if ctx.scheme not in ("postgresql", "mysql"):
            raise InvalidConfig("Unsupported database scheme '{}'".format(ctx.scheme))
        if ctx.retention:
            ctx.retention = int(ctx.retention)

    def _get_command(self, action):
        ctx = self.context
        param_map = {
            "hostname": "--host",
            "username": "--user",
        }
        if ctx.scheme == "postgresql":
            cmd = (
                ["pg_dump"]
                if action == ACTION_BACKUP
                else ["psql", "--quiet -o /dev/null"]
            )
            if ctx.password:
                ctx.env["PGPASSWORD"] = ctx.password
            param_map["dbname"] = "dbname"
        elif ctx.scheme == "mysql":
            cmd = (
                ["mysqldump", "--no-tablespaces"]
                if action == ACTION_BACKUP
                else ["mysql"]
            )
            param_map["password"] = "--password"
            if ctx.dbname:
                cmd.append("{dbname}")

        for field, param in param_map.items():
            value = ctx.get(field)
            if value is not None:
                cmd.append("%s={%s}" % (param, field))
        result = " ".join(cmd)
        if ctx.su_user:
            result = 'su {} -c"{}"'.format(ctx.su_user, result)
        return result

    def backup(self, report):
        ctx = self.context
        ctx.current_date = datetime.now().isoformat()
        ctx.dump_basename = "{scheme}_{current_date}.sql".format(**ctx)
        ctx.dump_filename = "/tmp/{dump_basename}".format(**ctx)
        ctx.dump_filename_zip = ctx.dump_filename + ".tar.gz"
        ctx.dump_dirname = os.path.dirname(ctx.dump_filename)

        self.run(self._get_command(ACTION_BACKUP) + " > {dump_filename}")
        self.run("tar -C {dump_dirname} -zcvf {dump_filename_zip} {dump_basename}")
        self.run("{aws_cli} s3 cp {dump_filename_zip} {s3_bucket_url}/{dump_basename}")
        report.add(ctx.name, ["upload: {s3_bucket_url}/{dump_basename}".format(**ctx)])
        self.run("rm {dump_filename} {dump_filename_zip}")
        if ctx.retention:
            result = self.run(
                "{aws_cli} s3api list-objects-v2 --bucket {bucket} --prefix {s3_path}/{scheme}_ --query 'sort_by(Contents, &LastModified)[].Key' --output=json"
            )
            target_file_list = json.loads(result)
            if len(target_file_list) > ctx.retention:
                for filename in target_file_list[: -ctx.retention]:
                    self.run("{aws_cli} s3 rm s3://{bucket}/" + filename)
                    report.add(
                        ctx.name, ["delete: s3://{bucket}/".format(**ctx) + filename]
                    )

    def restore(self):
        ctx = self.context
        result = self.run(
            "{aws_cli} s3api list-objects-v2 --bucket {bucket} --prefix {s3_path}/{scheme}_ --query 'sort_by(Contents, &LastModified)[-1].Key' --output=json",
        )
        target_file = json.loads(result)
        if target_file:
            ctx.dump_basename = os.path.basename(target_file)
            ctx.dump_filename = "/tmp/" + ctx.dump_basename
            ctx.dump_filename_zip = ctx.dump_filename + ".tar.gz"
        else:
            raise BackupNotFound()
        self.run("{aws_cli} s3 cp {s3_bucket_url}/{dump_basename} {dump_filename_zip}")
        ctx.dump_filename = self.run(
            "tar --force-local -zvxf {dump_filename_zip}"
        ).strip("\n")
        self.run(self._get_command(ACTION_RESTORE) + " < {dump_filename}")
        self.run("rm {dump_filename} {dump_filename_zip}")


@JobManager.register("smtp")
class SMTPJob(BaseJob):
    fields = (
        "server",
        "port",
        "username",
        "password",
        "sender",
        "recipients",
        "subject",
        "ssl",
    )

    def __init__(self, section):
        super().__init__(section)
        ctx = self.context
        if ctx.sender is None:
            ctx.sender = ctx.username
        if ctx.subject is None:
            ctx.subject = "Backup report: " + ctx.hostname
        ctx.current_date = datetime.now().ctime()

    def backup(self, report):
        ctx = self.context
        title = "Backup report for {hostname}: {result}".format(
            result="Error" if report.error else "Success", **ctx
        )
        li_item = "<p>{}</p>"
        body = """\
            <h1>{title}</h1>
            <p>{current_date}</p>
        """.format(
            title=title, **ctx
        )
        for section, items in report.items():
            body += "<h2><pre>{}</pre></h2>".format(section)
            body += "\n".join(li_item.format(item) for item in items)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = title
        msg["From"] = ctx.sender
        msg["To"] = ctx.recipients
        mime_text_body = MIMEText(
            re.sub("<[^<]+?>", "", re.sub("</[^<]+?>", "\n", body)), "text"
        )  # cries in xml
        mime_html_body = MIMEText(body, "html")
        msg.attach(mime_text_body)
        msg.attach(mime_html_body)

        logger.info("Sending report email to {}".format(ctx.recipients))
        if ctx.ssl:
            conn = smtplib.SMTP_SSL(
                ctx.server, port=ctx.port, context=ssl.create_default_context()
            )
        else:
            conn = smtplib.SMTP(ctx.server, port=ctx.port)
        conn.login(ctx.username, ctx.password)
        conn.sendmail(ctx.sender, ctx.recipients.split(","), msg.as_string())
        conn.quit()

    def restore(self):
        pass


class NestBackupCommand:
    def __init__(self, action, force=False):
        self.action = action
        self.force = force
        self.config_filename = os.getenv(
            "NESTBACKUP_CONFIG", os.path.expanduser("~/backup.ini")
        )

    def start(self):
        if self.action == ACTION_INIT:
            if os.path.exists(self.config_filename):
                logger.error("{} is not empty".format(self.config_filename))
                sys.exit(1)
            else:
                with open(self.config_filename, "w") as f:
                    f.write(CONFIG_EXAMPLE_TEMPLATE)
                os.chmod(self.config_filename, 0o600)
                logger.info("{} created".format(self.config_filename))
                return
        elif self.action == ACTION_RESTORE:
            if not (
                self.force
                or input("Please type the `hostname` to confirm the action: ")
                == socket.gethostname()
            ):
                logger.error("Restore aborted")
                sys.exit(1)

        config = configparser.ConfigParser()
        config.read(self.config_filename)

        report = Report()
        for name in config.sections():
            section = config[name]
            job_class = JobManager.get(section["job"])
            if job_class:
                job = job_class(section)
                try:
                    if self.action == ACTION_BACKUP:
                        job.backup(report)
                    elif self.action == ACTION_RESTORE:
                        job.restore()
                    elif self.action == ACTION_VALIDATE:
                        logger.info("Config section '{}' is valid.".format(name))
                    else:
                        raise ValueError("Invalid action")
                except Exception as e:
                    error_msg = "Error when running {} section: {}".format(name, e)
                    logger.error(error_msg, exc_info=e)
                    report.error = True
                    report.add(name, [error_msg])
            else:
                raise InvalidConfig(section["job"])
        if report.error:
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Backup a server to S3 using a short and simple config file. If NESTBACKUP_CONFIG environment variable is not set, it takes ~/backup.ini by default."
    )
    parser.add_argument(
        "action", choices=ACTION_CHOICES, help="Backup, restore, or validate the config"
    )
    parser.add_argument(
        "--force", "-f", action="store_true", default=False, help="Don't ask user input"
    )
    parser.add_argument("--verbose", "-v", action="count", default=0)
    args = parser.parse_args()

    log_level = logging.WARNING
    if args.verbose == 1:
        log_level = logging.INFO
    elif args.verbose > 1:
        log_level = logging.DEBUG
    logging.basicConfig(level=log_level)

    NestBackupCommand(args.action, args.force).start()


if __name__ == "__main__":
    main()
