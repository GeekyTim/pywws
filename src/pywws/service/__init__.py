# pywws - Python software for USB Wireless Weather Stations
# http://github.com/jim-easterbrook/pywws
# Copyright (C) 2018  pywws contributors

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

from __future__ import absolute_import, print_function, unicode_literals

from collections import deque
from datetime import datetime, timedelta
import os
import sys
import threading

if sys.version_info[0] >= 3:
    from io import StringIO
else:
    from StringIO import StringIO

import pywws
import pywws.logger
import pywws.storage
import pywws.template


class UploadThread(threading.Thread):
    def __init__(self, parent, context):
        super(UploadThread, self).__init__()
        self.parent = parent
        self.context = context
        self.queue = deque()

    def run(self):
        self.parent.logger.debug('thread started ' + self.name)
        self.old_message = ''
        if self.context.live_logging:
            polling_interval = self.parent.interval.total_seconds() / 20
            polling_interval = min(max(polling_interval, 4.0), 40.0)
        else:
            polling_interval = 4.0
        while not self.context.shutdown.is_set():
            try:
                OK = self.upload_batch()
            except Exception as ex:
                self.log(str(ex))
                OK = False
            if OK:
                pause = polling_interval
            elif self.context.live_logging:
                # upload failed, wait before trying again
                pause = 40.0
            else:
                # upload failed or nothing more to do
                break
            self.context.shutdown.wait(pause)

    def stop(self):
        if self.is_alive():
            self.parent.logger.debug('stopping thread ' + self.name)
            self.queue.append(None)

    def upload_batch(self):
        if not self.queue:
            return True
        OK = True
        count = 0
        with self.parent.session() as session:
            while self.queue and not self.context.shutdown.is_set():
                if self.parent.catchup == 0:
                    # "live only" service, so ignore old records
                    drop = len(self.queue) - 1
                    if self.queue[-1] is None:
                        drop -= 1
                    if drop > 0:
                        for i in range(drop):
                            self.queue.popleft()
                        self.parent.logger.warning(
                            '{:d} record(s) dropped'.format(drop))
                # send upload without taking it off queue
                upload = self.queue[0]
                if upload is None:
                    OK = False
                    break
                timestamp, kwds = upload
                OK, message = self.parent.upload_data(session, **kwds)
                self.log(message)
                if not OK:
                    break
                count += 1
                if timestamp:
                    self.context.status.set(
                        'last update', self.parent.service_name, str(timestamp))
                # finally remove upload from queue
                self.queue.popleft()
        if self.parent.log_count:
            if count > 1:
                self.parent.logger.warning('{:d} records sent'.format(count))
            elif count:
                self.parent.logger.info('1 record sent')
        return OK

    def log(self, message):
        if message == self.old_message:
            self.parent.logger.debug(message)
        else:
            self.parent.logger.error(message)
            self.old_message = message


class ServiceBase(threading.Thread):
    interval = timedelta(seconds=40)

    def __init__(self, context):
        super(ServiceBase, self).__init__()
        self.context = context
        self.queue = deque()

    def run(self):
        self.logger.debug('thread started ' + self.name)
        self.old_message = ''
        if self.context.live_logging:
            polling_interval = self.interval.total_seconds() / 20
            polling_interval = min(max(polling_interval, 4.0), 40.0)
        else:
            polling_interval = 4.0
        while not self.context.shutdown.is_set():
            OK = True
            if self.queue:
                try:
                    OK = self.upload_batch()
                except Exception as ex:
                    self.log(str(ex))
                    OK = False
            if OK:
                pause = polling_interval
            elif self.context.live_logging:
                # upload failed, wait before trying again
                pause = 40.0
            else:
                # upload failed or nothing more to do
                break
            self.context.shutdown.wait(pause)

    def stop(self):
        if self.is_alive():
            self.logger.debug('stopping thread ' + self.name)
            self.queue.append(None)

    def log(self, message):
        if message == self.old_message:
            self.logger.debug(message)
        else:
            self.logger.error(message)
            self.old_message = message


class DataServiceBase(ServiceBase):
    def __init__(self, context):
        super(DataServiceBase, self).__init__(context)
        # check config
        template = context.params.get(self.service_name, 'template')
        if template == 'default':
            context.params.unset(self.service_name, 'template')
        elif template:
            self.logger.critical(
                'obsolete item "template" found in weather.ini '
                'section [{}]'.format(self.service_name))
        # create templater
        if self.template:
            self.templater = pywws.template.Template(context, use_locale=False)
            self.template_file = StringIO(self.template)
        # get time stamp of last uploaded data
        self.last_update = self.context.status.get_datetime(
            'last update', self.service_name)

    def do_catchup(self):
        pass

    def prepare_data(self, data):
        data_str = self.templater.make_text(self.template_file, data)
        self.template_file.seek(0)
        return eval('{' + data_str + '}')

    def valid_data(self, data):
        return True


class DataService(DataServiceBase):
    catchup = 7

    def upload(self, catchup=True, live_data=None, test_mode=False, option=''):
        OK = True
        count = 0
        for data, live in self.next_data(catchup and not test_mode, live_data):
            if count >= 30 or len(self.queue) >= 60:
                break
            timestamp = data['idx']
            if test_mode:
                timestamp = None
            prepared_data = self.prepare_data(data)
            prepared_data.update(self.fixed_data)
            self.queue.append(
                (timestamp, {'prepared_data': prepared_data, 'live': live}))
            count += 1
        # start upload thread
        if self.queue and not self.is_alive():
            self.start()

    def next_data(self, catchup, live_data):
        if not catchup:
            start = self.context.calib_data.before(datetime.max)
        elif self.last_update:
            start = self.last_update + self.interval
        else:
            start = datetime.utcnow() - max(
                timedelta(days=self.catchup), self.interval)
        if live_data:
            stop = live_data['idx'] - self.interval
        else:
            stop = None
        next_update = start or datetime.min
        for data in self.context.calib_data[start:stop]:
            if data['idx'] >= next_update and self.valid_data(data):
                yield data, False
                self.last_update = data['idx']
                next_update = self.last_update + self.interval
        if (live_data and live_data['idx'] >= next_update and
                self.valid_data(live_data)):
            yield live_data, True
            self.last_update = live_data['idx']

    def upload_batch(self):
        OK = True
        count = 0
        with self.session() as session:
            while self.queue and not self.context.shutdown.is_set():
                if self.catchup == 0:
                    # "live only" service, so ignore old records
                    drop = len(self.queue) - 1
                    if self.queue[-1] is None:
                        drop -= 1
                    if drop > 0:
                        for i in range(drop):
                            self.queue.popleft()
                        self.logger.warning(
                            '{:d} record(s) dropped'.format(drop))
                # send upload without taking it off queue
                upload = self.queue[0]
                if upload is None:
                    OK = False
                    break
                timestamp, kwds = upload
                OK, message = self.upload_data(session, **kwds)
                self.log(message)
                if not OK:
                    break
                count += 1
                if timestamp:
                    self.context.status.set(
                        'last update', self.service_name, str(timestamp))
                # finally remove upload from queue
                self.queue.popleft()
        if count > 1:
            self.logger.warning('{:d} records sent'.format(count))
        elif count:
            self.logger.info('1 record sent')
        return OK


class LiveDataService(DataServiceBase):
    def upload(self, live_data=None, test_mode=False, option=''):
        if live_data:
            data = live_data
        else:
            idx = self.context.calib_data.before(datetime.max)
            if not idx:
                return
            data = self.context.calib_data[idx]
        timestamp = data['idx']
        if test_mode:
            timestamp = None
        elif self.last_update and timestamp < self.last_update + self.interval:
            return
        if not self.valid_data(data):
            return
        self.last_update = data['idx']
        prepared_data = self.prepare_data(data)
        prepared_data.update(self.fixed_data)
        self.queue.append((timestamp, {'prepared_data': prepared_data,
                                       'live': bool(live_data)}))
        # start upload thread
        if self.queue and not self.is_alive():
            self.start()

    def upload_batch(self):
        # remove stale uploads from queue
        drop = len(self.queue) - 1
        if self.queue[-1] is None:
            drop -= 1
        if drop > 0:
            for i in range(drop):
                self.queue.popleft()
            self.logger.warning('{:d} record(s) dropped'.format(drop))
        # send upload without taking it off queue
        upload = self.queue[0]
        if upload is None:
            return False
        timestamp, kwds = upload
        with self.session() as session:
            OK, message = self.upload_data(session, **kwds)
        self.log(message)
        if OK:
            if timestamp:
                self.context.status.set(
                    'last update', self.service_name, str(timestamp))
            # finally remove upload from queue
            self.queue.popleft()
        return OK


class FileService(ServiceBase):
    def __init__(self, context):
        super(FileService, self).__init__(context)
        self.local_dir = context.params.get(
            'paths', 'local_files', os.path.expanduser('~/weather/results/'))

    def do_catchup(self):
        pending = eval(self.context.status.get(
            'pending', self.service_name, '[]'))
        if pending:
            self.upload(option='CATCHUP')

    def upload(self, live_data=None, option=''):
        self.queue.append(option)
        # start upload thread
        if self.queue and not self.is_alive():
            self.start()

    def upload_batch(self):
        # make list of files to upload
        pending = eval(self.context.status.get(
            'pending', self.service_name, '[]'))
        files = []
        while self.queue and not self.context.shutdown.is_set():
            upload = self.queue[0]
            if upload is None:
                break
            if upload == 'CATCHUP':
                for path in pending:
                    if path not in files:
                        files.append(path)
            else:
                if not os.path.isabs(upload):
                    upload = os.path.join(self.local_dir, upload)
                if upload not in files:
                    files.append(upload)
                if upload not in pending:
                    pending.append(upload)
            self.queue.popleft()
        # upload files
        OK = True
        with self.session() as session:
            for path in files:
                OK, message = self.upload_file(session, path)
                self.log(message)
                if OK:
                    pending.remove(path)
                else:
                    break
        self.context.status.set('pending', self.service_name, repr(pending))
        if self.queue and self.queue[0] is None:
            OK = False
        return OK


def main(class_, argv=None):
    import argparse
    import inspect

    if argv is None:
        argv = sys.argv
    docstring = inspect.getdoc(sys.modules[class_.__module__]).split('\n\n')
    parser = argparse.ArgumentParser(
        description=docstring[0], epilog=docstring[1])
    if hasattr(class_, 'register'):
        parser.add_argument('-r', '--register', action='store_true',
                            help='register (or update) with service')
    if issubclass(class_, DataService):
        parser.add_argument('-c', '--catchup', action='store_true',
                            help='upload all data since last upload')
    parser.add_argument('-v', '--verbose', action='count',
                        help='increase amount of reassuring messages')
    parser.add_argument('data_dir', help='root directory of the weather data')
    if issubclass(class_, FileService):
        parser.add_argument('file', nargs='*', help='file to be uploaded')
    args = parser.parse_args(argv[1:])
    pywws.logger.setup_handler(args.verbose or 0)
    with pywws.storage.pywws_context(args.data_dir) as context:
        uploader = class_(context)
        if 'register' in args and args.register:
            uploader.register()
            context.flush()
            return 0
        if issubclass(class_, FileService):
            for file in args.file:
                uploader.upload(option=os.path.abspath(file))
        elif issubclass(class_, LiveDataService):
            uploader.upload(test_mode=True)
        else:
            uploader.upload(catchup=args.catchup, test_mode=not args.catchup)
        uploader.stop()
    return 0
