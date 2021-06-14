# NestBackup

Backup a server to S3 using on a short and simple config file.

# Configuration

NestBackup expects the config file to be in `~/backup.ini`. It can be overriden using the `NESTBACKUP_CONFIG` environment variable.

Each section describes a job to be ran, and it is uploaded to `hostname/section_name/[remote_path]`.

Available jobs:
- sync: Sinchronizes a path with the remote one. Uses `s3 sync`.
- database: Dumps the data of given database, compresses it and uploads it to the bucket. It supports mysql and postgresql.
- smtp: Sends an email with a report of the jobs that were ran.

### Example config file

```
[DEFAULT]
# The DEFAULT section contains the default values set to all the jobs
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
server=smtp.mailgun.org
ssl=yes
port=465
username=postmaster@mg.example.com
password=secret123
sender=Example Systems <postmaster@mg.example.com>
recipients=admin@example.com
```


## Usage

Initialize a base config file in `~/backup.ini` (overridable by `NESTBACKUP_CONFIG` environment variable):
```
nestbackup init
```

Backup:
```
nestbackup backup
```

Restore:
```
nestbackup restore
```


### Cron example

```
  0 1     *  *  * bash -lc "nestbackup backup"
```


## Installation


```
pip3 install nestbackup
```

Or you can checkout the repository and install it using `setup.py`:

```
git checkout git@github.com:jorgebg/nestbackup.git
cd nestbackup
python3 ./setup.py install
```
Or download it right to your `/usr/bin/` folder:

```
wget https://raw.githubusercontent.com/jorgebg/nestbackup/main/nestbackup.py -o /usr/bin/nestbackup
chmod +x /usr/bin/nestbackup
```

### Requirements

- **Python 3.6+**, but I'm successfully using it on machines with Python 3.4
- **AWS CLI**
  - `pip install awscli`