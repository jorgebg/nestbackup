# syntax=docker/dockerfile:1

FROM python:3.6-slim-buster

WORKDIR /app

RUN apt-get update
RUN apt-get install -y postgresql-client default-mysql-client

COPY test/server/requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

WORKDIR /nestbackup
ADD ./ /nestbackup
RUN python3 setup.py install
RUN rm -rf /nestbackup