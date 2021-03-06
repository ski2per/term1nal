import os
import os.path


def get_bool_env(name, default=False) -> bool:
    """
    Return boolean value from environment varialbes, e.g.:
    ENV_NAME=true or ENV_NAME=1 will return True, or return False
    """
    result = default
    env_value = os.getenv(name)
    if env_value is not None:
        result = os.getenv(name).upper() in ("TRUE", "1")
    return result


class Conf(dict):
    def __init__(self, *args, **kwargs):
        super(Conf, self).__init__(*args, **kwargs)
        for arg in args:
            if isinstance(arg, dict):
                for k, v in arg.items():
                    self[k] = v
            if kwargs:
                for k, v in kwargs.items():
                    self[k] = v

    def __getattr__(self, item):
        return self.get(item)

    def __setattr__(self, key, value):
        self.__setitem__(key, value)

    def __setitem__(self, key, value):
        super(Conf, self).__setitem__(key, value)
        self.__dict__.update({key: value})

    def __delattr__(self, item):
        self.__delitem__(item)

    def __delitem__(self, key):
        super(Conf, self).__delitem__(key)
        del self.__dict__[key]


conf = Conf()
conf.host = os.getenv("TERM_HOST", "0.0.0.0")
conf.port = int(os.getenv("TERM_PORT", 8000))
conf.ssl_port = int(os.getenv("TERM_SSL_PORT", 4433))
conf.cert_file = os.getenv("TERM_CERT_FILE", "./ssl.crt")
conf.key_file = os.getenv("TERM_KEY_FILE", "./ssl.key")
conf.debug = get_bool_env("TERM_DEBUG", True)
conf.xsrf = get_bool_env("TERM_XSRF", False)
conf.origin = os.getenv("TERM_ORIGIN", "*")
conf.ws_ping = int(os.getenv("TERM_WS_PING", 0))
conf.timeout = int(os.getenv("TERM_TIMEOUT", 3))
conf.max_conn = int(os.getenv("TERM_MAX_CONN", 20))
conf.delay = int(os.getenv("TERM_MAX_CONN", 0))
conf.encoding = os.getenv("TERM_ENCODING", "")

