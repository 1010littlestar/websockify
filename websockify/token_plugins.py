import os
import sys
import time
import re

import threading
import asyncore
import websocket
import json
import socket
import urllib.request

class BasePlugin():
    def __init__(self, src):
        self.source = src

    def lookup(self, token):
        return None


class ReadOnlyTokenFile(BasePlugin):
    # source is a token file with lines like
    #   token: host:port
    # or a directory of such files
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._targets = None

    def _load_targets(self):
        if os.path.isdir(self.source):
            cfg_files = [os.path.join(self.source, f) for
                         f in os.listdir(self.source)]
        else:
            cfg_files = [self.source]

        self._targets = {}
        index = 1
        for f in cfg_files:
            for line in [l.strip() for l in open(f).readlines()]:
                if line and not line.startswith('#'):
                    try:
                        tok, target = re.split(':\s', line)
                        self._targets[tok] = target.strip().rsplit(':', 1)
                    except ValueError:
                        print("Syntax error in %s on line %d" % (self.source, index), file=sys.stderr)
                index += 1

    def lookup(self, token):
        if self._targets is None:
            self._load_targets()

        if token in self._targets:
            return self._targets[token]
        else:
            return None


# the above one is probably more efficient, but this one is
# more backwards compatible (although in most cases
# ReadOnlyTokenFile should suffice)
class TokenFile(ReadOnlyTokenFile):
    # source is a token file with lines like
    #   token: host:port
    # or a directory of such files
    def lookup(self, token):
        self._load_targets()

        return super().lookup(token)


class BaseTokenAPI(BasePlugin):
    # source is a url with a '%s' in it where the token
    # should go

    # we import things on demand so that other plugins
    # in this file can be used w/o unnecessary dependencies

    def process_result(self, resp):
        host, port = resp.text.split(':')
        port = port.encode('ascii','ignore')
        return [ host, port ]

    def lookup(self, token):
        import requests

        resp = requests.get(self.source % token)

        if resp.ok:
            return self.process_result(resp)
        else:
            return None


class JSONTokenApi(BaseTokenAPI):
    # source is a url with a '%s' in it where the token
    # should go

    def process_result(self, resp):
        resp_json = resp.json()
        return (resp_json['host'], resp_json['port'])


class JWTTokenApi(BasePlugin):
    # source is a JWT-token, with hostname and port included
    # Both JWS as JWE tokens are accepted. With regards to JWE tokens, the key is re-used for both validation and decryption.

    def lookup(self, token):
        try:
            from jwcrypto import jwt
            import json

            key = jwt.JWK()

            try:
                with open(self.source, 'rb') as key_file:
                    key_data = key_file.read()
            except Exception as e:
                print("Error loading key file: %s" % str(e), file=sys.stderr)
                return None

            try:
                key.import_from_pem(key_data)
            except:
                try:
                    key.import_key(k=key_data.decode('utf-8'),kty='oct')
                except:
                    print('Failed to correctly parse key data!', file=sys.stderr)
                    return None

            try:
                token = jwt.JWT(key=key, jwt=token)
                parsed_header = json.loads(token.header)

                if 'enc' in parsed_header:
                    # Token is encrypted, so we need to decrypt by passing the claims to a new instance
                    token = jwt.JWT(key=key, jwt=token.claims)

                parsed = json.loads(token.claims)
                
                if 'nbf' in parsed:
                    # Not Before is present, so we need to check it
                    if time.time() < parsed['nbf']:
                        print('Token can not be used yet!', file=sys.stderr)
                        return None

                if 'exp' in parsed:
                    # Expiration time is present, so we need to check it
                    if time.time() > parsed['exp']:
                        print('Token has expired!', file=sys.stderr)
                        return None

                return (parsed['host'], parsed['port'])
            except Exception as e:
                print("Failed to parse token: %s" % str(e), file=sys.stderr)
                return None
        except ImportError as e:
            print("package jwcrypto not found, are you sure you've installed it correctly?", file=sys.stderr)
            return None

class TokenRedis():
    """
    The TokenRedis plugin expects the format of the data in a form of json.
    
    Prepare data with:
        redis-cli set hello '{"host":"127.0.0.1:5000"}'
    
    Verify with:
        redis-cli --raw get hello
    
    Spawn a test "server" using netcat
        nc -l 5000 -v
    
    Note: you have to install also the 'redis' and 'simplejson' modules
          pip install redis simplejson
    """
    def __init__(self, src):
        try:
            # import those ahead of time so we provide error earlier
            import redis
            import simplejson
            self._server, self._port = src.split(":")
            print("TokenRedis backend initilized (%s:%s)" %
                  (self._server, self._port), file=sys.stderr)
        except ValueError:
            print("The provided --token-source='%s' is not in an expected format <host>:<port>" %
                  src, file=sys.stderr)
            sys.exit()
        except ImportError:
            print("package redis or simplejson not found, are you sure you've installed them correctly?", file=sys.stderr)
            sys.exit()

    def lookup(self, token):
        try:
            import redis
            import simplejson
        except ImportError:
            print("package redis or simplejson not found, are you sure you've installed them correctly?", file=sys.stderr)
            sys.exit()

        print("resolving token '%s'" % token, file=sys.stderr)
        client = redis.Redis(host=self._server, port=self._port)
        stuff = client.get(token)
        if stuff is None:
            return None
        else:
            responseStr = stuff.decode("utf-8")
            print("response from redis : %s" % responseStr, file=sys.stderr)
            combo = simplejson.loads(responseStr)
            (host, port) = combo["host"].split(':')
            print("host: %s, port: %s" % (host,port), file=sys.stderr)
            return [host, port]


class UnixDomainSocketDirectory(BasePlugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dir_path = os.path.abspath(self.source)

    def lookup(self, token):
        try:
            import stat

            if not os.path.isdir(self._dir_path):
                return None

            uds_path = os.path.abspath(os.path.join(self._dir_path, token))
            if not uds_path.startswith(self._dir_path):
                return None

            if not os.path.exists(uds_path):
                return None

            if not stat.S_ISSOCK(os.stat(uds_path).st_mode):
                return None

            return [ 'unix_socket', uds_path ]
        except Exception as e:
                print("Error finding unix domain socket: %s" % str(e), file=sys.stderr)
                return None

url_path = 'http://192.168.10.222'
device_dic = {}

class DeviceClient(asyncore.dispatcher_with_send):
    device_id = ""

    def handle_read(self):
        data = self.recv(8192)
        if data:
            try:
                js = json.loads(data.decode('utf-8'))
                self.device_id = js["device_id"]
                action = js["action"]
            except Exception as e:
                msg = '{"status": -1, "msg": "%s"}' % str(e)
                self.send(msg.encode('utf-8'))
                return

            if action != "request":
                msg = '{"status": -1, "msg": "not request"}'
                self.send(msg.encode('utf-8'))
                return

            port = self.get_free_port();
            if port is None:
                msg = '{"status": -1, "msg": "cant alloc port"}'
                self.send(msg.encode('utf-8'))
                return

            device_dic[self.device_id] = port
            msg = '{"status": 0, "msg": "", data: {device_id: %s, port: %s}}' % (self.device_id, port)

            self.send(msg.encode('utf-8'))

            # notify java client online
            url = url_path + '/api/device/device/editVNCStatus?deviceCode=%s&status=1' % self.device_id
            print(url)
            request = urllib.request.Request(url, method = 'POST')
            reponse = urllib.request.urlopen(request).read()

    def handle_close(self):
        print('client close')
        if self.device_id in device_dic.keys():
            del device_dic[self.device_id]

        # notify java client offline
        url = url_path + '/api/device/device/editVNCStatus?deviceCode=%s&status=0' % self.device_id
        print(url)
        request = urllib.request.Request(url, method = 'POST')
        reponse = urllib.request.urlopen(request).read()

        self.close()

    def get_free_port(self):
        for port in range(20000, 30000):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = s.connect_ex(("127.0.0.1", int(port)))
            if result:
                if port not in device_dic.values():
                    return port

        return None


class DeviceServer(asyncore.dispatcher):

    def __init__(self, host, port):
        asyncore.dispatcher.__init__(self)
        self.create_socket()
        self.set_reuse_addr()
        self.bind((host, port))
        self.listen(1000)
        self.listenport = 8765

    def handle_accepted(self, sock, addr):
        print('Incoming connection from %s' % repr(addr))
        Client = DeviceClient(sock)

    def handle_close(self):
        print('server close client')

class TokenDevice(BasePlugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._devices = {}
        self._thread = threading.Thread(target=self.start_device_server)
        self._thread.start()

    def start_device_server(self):
        self._server = DeviceServer("127.0.0.1", self.listenport)
        asyncore.loop()

    def lookup(self, token):
        print("resolving token '%s'" % token, file=sys.stderr)
        if token in device_dic.keys():
            return ["127.0.0.1", device_dic[token]]
        else:
            return None

