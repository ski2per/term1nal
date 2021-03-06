import re
import json
import socket
import struct
import os.path
import weakref
import paramiko
import tornado.web
from json.decoder import JSONDecodeError
from tornado import iostream
from tornado.ioloop import IOLoop
from concurrent.futures import ThreadPoolExecutor
from tornado.process import cpu_count

from term1nal.conf import conf
from term1nal.minion import Minion, recycle_minion, GRU
from term1nal.utils import LOG

DELAY = 3
DEFAULT_PORT = 22


class InvalidValueError(Exception):
    pass


class CommonMixin:
    fh = None
    args = None
    ssh_transport_client = None
    minion_id = None
    filename = ''

    def initialize(self, loop):
        self.context = self.request.connection.context
        self.loop = loop

    def create_ssh_client(self, args):
        print(args)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.client.MissingHostKeyPolicy)
        try:
            ssh.connect(*args, timeout=conf.timeout)
        except socket.error:
            raise ValueError('Unable to connect to {}:{}'.format(*args[:2]))
        except (paramiko.AuthenticationException, paramiko.ssh_exception.AuthenticationException):
            raise ValueError('Authentication failed.')
        return ssh

    def exec_remote_cmd(self, cmd, probe_cmd=None):
        """
        Execute command(cmd or probe-command) on remote host

        :param cmd: Command to execute
        :param probe_cmd: Probe command to execute before 'cmd'
        :return: None
        """
        client_ip = self.get_client_endpoint()[0]
        gru = GRU.get(client_ip, {})
        args = gru[self.minion_id]["args"]

        self.ssh_transport_client = self.create_ssh_client(args)

        # Use probe_cmd to detect file's existence
        if probe_cmd:
            chan = self.ssh_transport_client.get_transport().open_session()
            chan.exec_command(probe_cmd)
            ext = (chan.recv_exit_status())
            if ext:
                raise tornado.web.HTTPError(404, "Not found")

        transport = self.ssh_transport_client.get_transport()
        self.fh = transport.open_channel(kind='session')
        self.fh.exec_command(cmd)

    def get_value(self, name, type=""):

        if type == "query":
            value = self.get_query_argument(name)
        else:
            value = self.get_argument(name)

        if not value:
            raise InvalidValueError(f'{name} is missing')
        return value

    def get_client_endpoint(self) -> tuple:
        """
        Return client endpoint

        :return: (IP, Port) tuple
        """
        ip = self.request.remote_ip

        if ip == self.request.headers.get("X-Real-Ip"):
            port = self.request.headers.get("X-Real-Port")
        elif ip in self.request.headers.get("X-Forwarded-For", ""):
            port = self.request.headers.get("X-Forwarded-Port")
        else:
            return self.context.address[:2]
        port = int(port)
        return ip, port


class StreamUploadMixin(CommonMixin):
    content_type = None
    boundary = None

    def _get_boundary(self):
        """
        Return the boundary of multipart/form-data

        :return: FormData boundary or None if not found
        """
        self.content_type = self.request.headers.get('Content-Type', '')
        match = re.match('.*;\sboundary="?(?P<boundary>.*)"?$', self.content_type.strip())
        if match:
            return match.group('boundary')
        else:
            return None

    def _write_chunk(self, chunk):
        trimmed_chunk = self._filter_trailing_carriage_return(chunk)
        self.fh.sendall(trimmed_chunk)

    @staticmethod
    def _filter_trailing_carriage_return(chunk):
        """
        Filter out trailing carriage return(\r\n),
        Not to use rstrip(), to make sure b'hello\n\r\n' won't become b'hello'
        Use str.rpartition() instead of str.rstrip to avoid b'hello\n\r\n' being stripped to b'hello'

        :param chunk:  Bytes string
        :return: Bytes string with '\r\n' filtered out
        """
        if chunk.endswith("\r\n".encode()):
            data, _, _ = chunk.rpartition('\r\n'.encode())
            return data
        return chunk

    def data_received(self, data):
        """

        :param data:
        :return: None
        """
        if not self.boundary:
            self.boundary = self._get_boundary()

        # Split data with multipart/form-data boundary
        sep = f'--{self.boundary}'
        chunks = data.split(sep.encode())

        for chunk in chunks:
            chunk_length = len(chunk)

            # If chunk length is 0, which means the data received is the beginning of multipart/form-data
            if chunk_length == 0:
                pass
            # Chunk length is 4, means the data received is end of multipart/form-data
            elif chunk_length == 4:
                # End, close file handler(or similar object)
                self.ssh_transport_client.close()
            else:
                need2partition = re.match('.*Content-Disposition:\sform-data;.*', chunk.decode('ISO-8859-1'),
                                          re.DOTALL | re.MULTILINE)
                if need2partition:
                    header, _, part = chunk.partition('\r\n\r\n'.encode('ISO-8859-1'))
                    if part:
                        header_text = header.decode('ISO-8859-1').strip()
                        if 'minion' in header_text:
                            self.minion_id = part.decode('ISO-8859-1').strip()

                        if 'upload' in header_text:
                            m = re.match('.*filename="(?P<filename>.*)".*', header_text, re.MULTILINE | re.DOTALL)
                            if m:
                                self.filename = m.group('filename')
                            else:
                                self.filename = 'untitled'

                            self.filename = re.sub('\s+', '_', self.filename)
                            # A trick to create a remote file handler
                            self.exec_remote_cmd(f'cat > /tmp/{self.filename}')
                            self._write_chunk(part)
                else:
                    self._write_chunk(chunk)


class IndexHandler(CommonMixin, tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(max_workers=cpu_count() * 5)

    def initialize(self, loop):
        print("indexhandler init")
        super(IndexHandler, self).initialize(loop=loop)
        # self.ssh_client = self.get_ssh_client()
        self.ssh_term_client = None
        self.debug = self.settings.get('debug', False)
        self.result = dict(id=None, status=None, encoding=None)

    def get_args(self):
        hostname = self.get_value('hostname')
        port = int(self.get_value('port'))
        username = self.get_value('username')
        password = self.get_value('password', '')
        args = (hostname, port, username, password)
        LOG.debug(args)
        return args

    def get_server_encoding(self, ssh):
        try:
            _, stdout, _ = ssh.exec_command("locale charmap")
        except paramiko.SSHException as err:
            LOG.error(str(err))
        else:
            result = stdout.read().decode().strip()
            if result:
                return result

        LOG.warning('!!! Unable to detect default encoding')
        return 'utf-8'

    def create_minion(self, args):
        ssh = self.ssh_term_client
        ssh_endpoint = args[:2]
        LOG.info('Connecting to {}:{}'.format(*ssh_endpoint))

        term = self.get_argument('term', '') or 'xterm'
        shell_channel = ssh.invoke_shell(term=term)
        shell_channel.setblocking(0)
        minion = Minion(self.loop, ssh, shell_channel, ssh_endpoint)
        minion.encoding = conf.encoding if conf.encoding else self.get_server_encoding(ssh)
        return minion

    def get(self):
        self.render('index.html', debug=self.debug)

    @tornado.gen.coroutine
    def post(self):
        ip, port = self.get_client_endpoint()
        minions = GRU.get(ip, {})
        if minions and len(minions) >= conf.max_conn:
            raise tornado.web.HTTPError(406, 'Too many connections')

        try:
            args = self.get_args()
            self.ssh_term_client = self.create_ssh_client(args)
            future = self.executor.submit(self.create_minion, args)
            minion = yield future
        except InvalidValueError as err:
            # Catch error in self.get_args()
            raise tornado.web.HTTPError(400, str(err))
        except (ValueError, paramiko.SSHException,
                paramiko.ssh_exception.AuthenticationException) as err:
            self.result.update(status=str(err))
        else:
            if not minions:
                GRU[ip] = minions
            minion.src_addr = (ip, port)
            minions[minion.id] = {
                "minion": minion,
                "args": args
            }
            self.loop.call_later(conf.delay or DELAY, recycle_minion, minion)
            self.result.update(id=minion.id, encoding=minion.encoding)
        self.write(self.result)


class WSHandler(CommonMixin, tornado.websocket.WebSocketHandler):

    def initialize(self, loop):
        super(WSHandler, self).initialize(loop=loop)
        self.minion_ref = None

    def open(self):
        self.src_addr = self.get_client_endpoint()
        LOG.info('Connected from {}:{}'.format(*self.src_addr))

        minions = GRU.get(self.src_addr[0])
        if not minions:
            self.close(reason='Websocket authentication failed.')
            return

        try:
            # Get id from query argument from
            minion_id = self.get_value('id')
            LOG.debug(f"############ minion id: {minion_id}")

        except (tornado.web.MissingArgumentError, InvalidValueError) as err:
            self.close(reason=str(err))
        else:
            minion = minions.get(minion_id)["minion"]
            if minion:
                minions[minion_id]["minion"] = None
                self.set_nodelay(True)
                minion.set_handler(self)
                self.minion_ref = weakref.ref(minion)
                self.loop.add_handler(minion.fd, minion, IOLoop.READ)
            else:
                self.close(reason='Websocket authentication failed.')

    def on_message(self, message):
        LOG.debug(f'{message} from {self.src_addr}')
        minion = self.minion_ref()
        try:
            msg = json.loads(message)
        except JSONDecodeError:
            return

        if not isinstance(msg, dict):
            return

        resize = msg.get('resize')
        if resize and len(resize) == 2:
            try:
                minion.chan.resize_pty(*resize)
            except (TypeError, struct.error, paramiko.SSHException):
                pass

        data = msg.get('data')
        if data and isinstance(data, str):
            minion.data_to_dst.append(data)
            minion.do_write()

    def on_close(self):
        LOG.info('Disconnected from {}:{}'.format(*self.src_addr))
        if not self.close_reason:
            self.close_reason = 'client disconnected'

        minion = self.minion_ref() if self.minion_ref else None
        if minion:
            minion.close(reason=self.close_reason)


@tornado.web.stream_request_body
class UploadHandler(StreamUploadMixin, CommonMixin, tornado.web.RequestHandler):
    def initialize(self, loop):
        super(UploadHandler, self).initialize(loop=loop)

    async def post(self):
        print("upload ended")
        await self.finish(f'/tmp/{self.filename}')  # Send filename back


class DownloadHandler(CommonMixin, tornado.web.RequestHandler):
    def initialize(self, loop):
        super(DownloadHandler, self).initialize(loop=loop)

    async def get(self):
        chunk_size = 1024 * 1024 * 1  # 1 MiB

        remote_file_path = self.get_value("filepath", type="query")
        filename = os.path.basename(remote_file_path)
        print(remote_file_path)
        self.minion_id = self.get_value("minion")
        print(f"minion ID: {self.minion_id}")
        client_ip = self.get_client_endpoint()[0]
        gru = GRU.get(client_ip, {})
        self.args = gru[self.minion_id]["args"]

        try:
            self.exec_remote_cmd(cmd=f'cat {remote_file_path}', probe_cmd=f'ls {remote_file_path}')
        except tornado.web.HTTPError:
            self.write(f'Not found: {remote_file_path}')
            await self.finish()

        self.set_header("Content-Type", "application/octet-stream")
        self.set_header("Accept-Ranges", "bytes")
        self.set_header("Content-Disposition", f"attachment; filename={filename}")

        while True:
            chunk = self.fh.recv(chunk_size)
            if not chunk:
                break
            try:
                # Write the chunk to response
                self.write(chunk)
                # Send the chunk to client
                await self.flush()
            except iostream.StreamClosedError:
                break
            finally:
                del chunk
                await tornado.web.gen.sleep(0.000000001)  # 1 nanosecond

        self.ssh_transport_client.close()
        await self.finish()
        print("download ended")
