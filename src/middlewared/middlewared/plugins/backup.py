from middlewared.schema import accepts, Int, Str
from middlewared.service import CRUDService, Service, item_method, job, private

import boto3
import gevent
import gevent.fileobject
import os
import subprocess
import re
import tempfile

CHUNK_SIZE = 5 * 1024 * 1024


class BackupService(CRUDService):

    @item_method
    @accepts(Int('id'))
    @job(lock=lambda args: 'backup:{}'.format(args[-1]))
    def sync(self, job, id):

        backup = self.middleware.call('datastore.query', 'tasks.cloudsync', [('id', '=', id)], {'get': True})
        if not backup:
            raise ValueError("Unknown id")

        credential = self.middleware.call('datastore.query', 'system.cloudcredentials', [('id', '=', backup['credential']['id'])], {'get': True})
        if not credential:
            raise ValueError("Backup credential not found.")

        if credential['provider'] == 'AMAZON':
            return self.middleware.call('backup.s3.sync', job, backup, credential)
        else:
            raise NotImplementedError('Unsupported provider: {}'.format(
                credential['provider']
            ))


class BackupS3Service(Service):

    class Config:
        namespace = 'backup.s3'

    @private
    def get_client(self, id):
        credential = self.middleware.call('datastore.query', 'system.cloudcredentials', [('id', '=', id)], {'get': True})

        client = boto3.client(
            's3',
            aws_access_key_id=credential['attributes'].get('access_key'),
            aws_secret_access_key=credential['attributes'].get('secret_key'),
        )
        return client

    @accepts(Int('id'))
    def get_buckets(self, id):
        """Returns buckets from a given S3 credential."""
        client = self.get_client(id)
        buckets = []
        for bucket in client.list_buckets()['Buckets']:
            buckets.append({
                'name': bucket['Name'],
                'creation_date': bucket['CreationDate'],
            })

        return buckets

    @accepts(Int('id'), Str('name'))
    def get_bucket_location(self, id, name):
        client = self.get_client(id)
        response = client.get_bucket_location(Bucket=name)
        return response['LocationConstraint']

    @private
    def sync(self, job, backup, credential):
        # Use a temporary file to store s3cmd config file
        with tempfile.NamedTemporaryFile() as f:
            # Make sure only root can read it ad there is sensitive data
            os.chmod(f.name, 0o600)

            fg = gevent.fileobject.FileObject(f.file, 'w', close=False)
            fg.write("""[remote]
type = s3
env_auth = false
access_key_id = {access_key}
secret_access_key = {secret_key}
region = {region}
""".format(
                access_key=credential['attributes']['access_key'],
                secret_key=credential['attributes']['secret_key'],
                region=backup['attributes']['region'] or '',
            ))
            fg.flush()

            args = [
                '/usr/local/bin/rclone',
                '--config', f.name,
                '--stats', '1s',
                'sync',
                backup['path'],
                'remote:{}'.format(backup['attributes']['bucket']),
            ]

            def check_progress(job, proc):
                RE_TRANSF = re.compile(r'Transferred:\s*?(.+)$', re.S)
                while True:
                    read = proc.stderr.readline()
                    if read == b'':
                        break
                    reg = RE_TRANSF.search(read)
                    if reg:
                        transferred = reg.group(1).strip()
                        if not transferred.isdigit():
                            job.set_progress(None, transferred)

            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            gevent.spawn(check_progress, job, proc)
            stderr = proc.communicate()[1]
            if proc.returncode != 0:
                raise ValueError('rclone failed: {}'.format(stderr))
            return True

    @private
    def put(self, backup, filename, read_fd):
        client = self.get_client(backup['id'])
        folder = backup['attributes']['folder'] or ''
        key = os.path.join(folder, filename)
        parts = []
        idx = 1

        try:
            with os.fdopen(read_fd, 'rb') as f:
                fg = gevent.fileobject.FileObject(f, 'rb', close=False)
                mp = client.create_multipart_upload(
                    Bucket=backup['attributes']['bucket'],
                    Key=key
                )

                while True:
                    chunk = fg.read(CHUNK_SIZE)
                    if chunk == b'':
                        break

                    resp = client.upload_part(
                        Bucket=backup['attributes']['bucket'],
                        Key=key,
                        PartNumber=idx,
                        UploadId=mp['UploadId'],
                        ContentLength=CHUNK_SIZE,
                        Body=chunk
                    )

                    parts.append({
                        'ETag': resp['ETag'],
                        'PartNumber': idx
                    })

                    idx += 1

                client.complete_multipart_upload(
                    Bucket=backup['attributes']['bucket'],
                    Key=key,
                    UploadId=mp['UploadId'],
                    MultipartUpload={
                        'Parts': parts
                    }
                )
        finally:
            pass

    @private
    def get(self, backup, filename, write_fd):
        client = self.get_client(backup['id'])
        folder = backup['attributes']['folder'] or ''
        key = os.path.join(folder, filename)
        obj = client.get_object(
            Bucket=backup['attributes']['bucket'],
            Key=key
        )

        with os.fdopen(write_fd, 'wb') as f:
            fg = gevent.fileobject.FileObject(f, 'wb', close=False)
            while True:
                chunk = obj['Body'].read(CHUNK_SIZE)
                if chunk == b'':
                    break
                fg.write(chunk)
