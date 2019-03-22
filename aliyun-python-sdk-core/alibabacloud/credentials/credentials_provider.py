import json
import os
import time

from aliyunsdkcore.acs_exception.exceptions import ClientException
from alibabacloud.credentials.credentials import AccessKeyCredentials, BearTokenCredentials, \
    SecurityCredentials

from aliyunsdkcore.acs_exception import error_code
from aliyunsdkcore.acs_exception import exceptions
from aliyunsdkcore.request import CommonRequest
from aliyunsdkcore.compat import ensure_string
from aliyunsdkcore.acs_exception import error_msg
from aliyunsdkcore.request import RpcRequest
from aliyunsdkcore.auth.algorithm import sha_hmac256

from alibabacloud.utils.ini_helper import load_config


# from ecs ram role get SecurityCredential
class InstanceMetadataFetcher(object):
    _REFRESH_SCALE = 0.8
    _URL_PATH = 'http://100.100.100.200/latest/meta-data/ram/security-credentials/'

    def __init__(self, ecs_ram_role):
        self.ecs_ram_role = ecs_ram_role
        self._last_update_time = 0
        self._expiration = 0

    @property
    def is_expiration(self):
        now = int(time.time())
        return now - self._last_update_time > (self._expiration * self._REFRESH_SCALE)

    def get_profile(self):
        if self.is_expiration:
            from aliyunsdkcore.vendored import requests
            r = requests.get(url=self._URL_PATH + self.ecs_ram_role)
            credentials = json.loads(r.text)
            if credentials.get("Code") != "Success":
                message = 'refresh Ecs sts token err, code is ' + \
                          credentials.get("Code")
                raise ServerException(
                    credentials.get("Code"), message, None)

            expiration = credentials.get("Expiration")
            if expiration:
                self._expiration = time.mktime(time.strptime(expiration, '%Y-%m-%dT%H:%M:%SZ'))
            else:
                self._expiration = expiration
            self._last_update_time = int(time.time())
            return {
                # 'role_name': self.ecs_ram_role,
                'access_key_id': credentials['AccessKeyId'],
                'access_key_secret': credentials['AccessKeySecret'],
                'security_token': credentials['SecurityToken'],
                # 'expiry_time': self._expiration,
            }
        return {}


# from rsa to AccessKeyCredential
class GetSessionAkRequest(RpcRequest):
    def __init__(self):
        RpcRequest.__init__(self, product='Sts', version='2015-04-01',
                            action_name='GenerateSessionAccessKey',
                            signer=sha_hmac256)
        self.set_protocol_type('https')

    def get_duration_seconds(self):
        return self.get_query_params().get("DurationSeconds")

    def set_duration_seconds(self, duration_seconds):
        self.add_query_param('DurationSeconds', duration_seconds)

    def get_public_key_id(self):
        return self.get_query_params().get('PublicKeyId')

    def set_public_key_id(self, public_key_id):
        self.add_query_param('PublicKeyId', public_key_id)


class RsaKeyPairFetcher(object):
    _MIN_SESSION_PERIOD = 900
    _MAX_SESSION_PERIOD = 3600
    REGION_ID = 'ap-northeast-1'

    def __init__(self, profile):
        session_period = profile.get('session_period')
        if session_period < self._MIN_SESSION_PERIOD or \
           session_period > self._MAX_SESSION_PERIOD:
            raise exceptions.ClientException(
                error_code.SDK_INVALID_SESSION_EXPIRATION,
                error_msg.get_msg('SDK_INVALID_SESSION_EXPIRATION').format(
                    self._MIN_SESSION_PERIOD,
                    self._MAX_SESSION_PERIOD))
        self._public_key_id = profile.get('public_key_id')
        self._private_key = profile.get('private_key_file')
        self._session_period = profile.session_period
        self._last_update_time = 0
        # self._schedule_interval = credential.session_period if debug \
        #     else max(credential.session_period * 0.8, 5)
        from aliyunsdkcore.client import AcsClient
        self._sts_client = AcsClient(
            self._public_key_id, self._private_key, self.REGION_ID)
        self._session_credential = {}

    @property
    def is_expiration(self):
        now = int(time.time())
        return now - self._last_update_time > (self._session_period * 0.8)

    def get_profile(self):
        if self.is_expiration:
            request = GetSessionAkRequest()
            request.set_method("GET")
            request.set_duration_seconds(self._session_period)

            try:
                response_str = self._sts_client.do_action_with_exception(request)
                response = json.loads(response_str.decode('utf-8'))
                session_ak = str(response.get(
                    "SessionAccessKey").get("SessionAccessKeyId"))
                session_sk = str(response.get(
                    "SessionAccessKey").get("SessionAccessKeySecret"))
                self._last_update_time = int(time.time())

                self._session_credential['access_key_id'] = session_ak
                self._session_credential['access_key_secret'] = session_sk

                return self._session_credential
            except exceptions.ServerException as srv_ex:
                if srv_ex.error_code == 'InvalidAccessKeyId.NotFound' or \
                   srv_ex.error_code == 'SignatureDoesNotMatch':
                    raise exceptions.ClientException(error_code.SDK_INVALID_CREDENTIAL,
                                                     error_msg.get_msg('SDK_INVALID_CREDENTIAL'))
                else:
                    raise
        return {}


# from ram role arn to SecurityCredential
class RamRoleArnFetcher(object):
    _SESSION_PERIOD = 3600
    _REFRESH_SCALE = 0.8
    _RETRY_DELAY_FAST = 3
    _PRIORITY = 1

    def __init__(self, profile):
        self._credential = profile
        self._last_update_time = 0
        self._session_credential = {}
        if len(profile.get('role_session_name')) == 0:
            self._credential.session_role_name = "aliyun-python-sdk-" + str(time.time())

    @property
    def is_expiration(self):
        now = int(time.time())
        return now - self._last_update_time > (self._SESSION_PERIOD * self._REFRESH_SCALE)

    def get_profile(self):
        if self.is_expiration:
            client = self._create_client()

            request = CommonRequest(product="Sts", version='2015-04-01', action_name='AssumeRole')
            request.set_method('POST')
            request.set_protocol_type('https')
            request.add_query_param('RoleArn', self._credential.get('role_arn'))
            request.add_query_param('RoleSessionName', self._credential.get('session_role_name'))
            request.add_query_param('DurationSeconds', self._SESSION_PERIOD)
            request.set_accept_format('JSON')

            body = client.handle_request(request)
            response = json.loads(body)
            session_ak = response.get("Credentials").get("AccessKeyId")
            session_sk = response.get("Credentials").get("AccessKeySecret")
            token = response.get("Credentials").get("SecurityToken")
            self._last_update_time = int(time.time())
            self._session_credential['access_key_id'] = session_ak
            self._session_credential['access_key_secret'] = session_sk
            self._session_credential['security_token'] = token
            return self._session_credential
        return {}

    def _create_client(self):
        """Create an STS client using the source credentials."""
        # 接受AK和token的方式传递
        from alibabacloud.client import ClientConfig, AlibabaCloudClient
        client_config = ClientConfig()
        client_config.access_key_id = self._credential.get('access_key_id')
        client_config.access_key_secret = self._credential.get('access_key_secret')
        client_config.region_id = 'cn-hangzhou'
        # FIXME :region_id should be fix
        client = AlibabaCloudClient(client_config)
        return client


# profile map to credentials
class ProfileResolve:

    def __init__(self, profile):
        self.profile = profile

    def get_resolve_func(self):
        mapping = {
            'access_key': self.resolve_type_of_access_key,
            'ecs_ram_role': self.resolve_type_of_ecs_ram_role,
            'ram_role_arn': self.resolve_type_of_ram_role_arn,
            'bearer_token': self.resolve_type_of_bearer_token,
            'rsa_key_pair': self.resolve_type_of_rsa_key_pair,
            'sts_token': self.resolve_type_of_security_token,
        }
        return mapping[self.profile['type']]

    def resolve_type_of_access_key(self):
        access_key_id = self.profile.get('access_key_id')
        access_key_secret = self.profile.get('access_key_secret')
        return AccessKeyCredentials(access_key_id, access_key_secret)

    def resolve_type_of_ecs_ram_role(self):
        fetcher = InstanceMetadataFetcher(self.profile)
        profile = fetcher.get_profile()
        return SecurityCredentials(**profile)

    def resolve_type_of_ram_role_arn(self):
        fetcher = RamRoleArnFetcher(self.profile)
        profile = fetcher.get_profile()
        return SecurityCredentials(**profile)

    def resolve_type_of_rsa_key_pair(self):
        fetcher = RsaKeyPairFetcher(**self.profile)
        profile = fetcher.get_profile()
        return AccessKeyCredentials(**profile)

    def resolve_type_of_bearer_token(self):
        bearer_token = self.profile.get('bearer_token')
        return BearTokenCredentials(bearer_token)

    def resolve_type_of_security_token(self):
        access_key_id = self.profile.get('access_key_id')
        access_key_secret = self.profile.get('access_key_secret')
        sts_token = self.profile.get('security_token')
        return SecurityCredentials(access_key_id, access_key_secret, sts_token)


# load config
class DefaultCredentialsProvider(object):
    def __init__(self, profile):
        profile_name = profile.get('profile_name') or 'default'
        providers = [
            StaticProvider(profile),
            EnvProvider(),
            ProfileCredentialsProvider(profile_name=profile_name),
            InstanceCredentialsProvider(),
        ]
        self.providers = providers

    def load_credentials(self):
        for provider in self.providers:
            credentials = provider.load()
            if credentials is not None:
                return credentials
        return None


class CredentialsProvider(object):

    # if no credentials got, return None
    def load(self):
        return None


class StaticProvider(CredentialsProvider):
    def __init__(self, profile):
        self.profile = profile

    def load(self):
        if self.profile:
            fetcher = self._create_credentials_fetcher()
            credentials = fetcher()
            return credentials
        else:
            return None

    def _create_credentials_fetcher(self):
        profile = self.profile

        def fetch_credentials():
            bearer_token = profile.get('bearer_token')
            if bearer_token is not None:
                return BearTokenCredentials(bearer_token)
            access_key_id = profile.get('access_key_id')
            if access_key_id is not None:
                access_key_secret = profile.get('access_key_secret')
                if access_key_secret is None:
                    raise ClientException(
                        'Credentials',
                        'Param access_key_secret can not be empty.')
                secret_token = profile.get('secret_token')
                if secret_token is not None:
                    return SecurityCredentials(access_key_id, access_key_secret, secret_token)
                return AccessKeyCredentials(access_key_id, access_key_secret)
            return None

        return fetch_credentials


class EnvProvider(CredentialsProvider):
    ENV_NAME_FOR_ACCESS_KEY_ID = 'ALIBABA_CLOUD_ACCESS_KEY_ID'
    ENV_NAME_FOR_ACCESS_KEY_SECRET = 'ALIBABA_CLOUD_ACCESS_KEY_SECRET'

    def __init__(self):
        self.environ = os.environ

    def load(self):
        if self.ENV_NAME_FOR_ACCESS_KEY_ID in self.environ:
            fetcher = self._create_credentials_fetcher()
            credentials = fetcher()
            return credentials
        else:
            return None

    def _create_credentials_fetcher(self):
        environ = self.environ

        def fetch_credentials():
            access_key_id = environ.get(self.ENV_NAME_FOR_ACCESS_KEY_ID)
            if access_key_id is None:
                raise ClientException(
                    'Credentials',
                    'Environment variable %s cannot be empty.' % self.ENV_NAME_FOR_ACCESS_KEY_ID)
            access_key_secret = environ.get(self.ENV_NAME_FOR_ACCESS_KEY_SECRET)
            if access_key_secret is None:
                raise ClientException(
                    'Credentials',
                    'Environment variable %s cannot be empty.' %
                    self.ENV_NAME_FOR_ACCESS_KEY_SECRET)

            credential = AccessKeyCredentials(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret)
            return credential

        return fetch_credentials


class ProfileCredentialsProvider(CredentialsProvider):
    ENV_NAME_FOR_CREDENTIALS_FILE = 'ALIBABA_CLOUD_CREDENTIALS_FILE'
    DEFAULT_NAME_FOR_CREDENTIALS_FILE = ['/etc/.alibabacloud/credentials',
                                         '~/alibabacloud/credentials']

    def __init__(self, profile_name=None):
        self._profile_name = profile_name
        self.environ = os.environ

    def load(self):
        self._loaded_config = {}

        if self.ENV_NAME_FOR_CREDENTIALS_FILE in self.environ:

            env_file_path = self.environ.get(self.ENV_NAME_FOR_CREDENTIALS_FILE)
            if env_file_path is None:
                raise ClientException(
                    'Credentials',
                    'The specified credential file path (%s) is invalid.' % env_file_path
                )
            full_path = os.path.expanduser(env_file_path)
            self._loaded_config = load_config(full_path)
            profile = self._loaded_config.get(self._profile_name, {})
            # can't find type
            if 'type' not in profile:
                raise ClientException(
                    'Credentials',
                    'The Credentials file (%s) can not find the needed param "type".' % full_path
                )
            return self._load_credentials_from_ini_type(profile)

        else:
            potential_locations = self.DEFAULT_NAME_FOR_CREDENTIALS_FILE
            for filename in potential_locations:
                try:
                    self._loaded_config = load_config(filename)
                    break
                except Exception:
                    continue
            profile = self._loaded_config.get(self._profile_name, {})

            if 'type' in profile:
                return self._load_credentials_from_ini_type(profile)
        return None

    @staticmethod
    def _load_credentials_from_ini_type(profile):

        fetcher = ProfileResolve(profile=profile)
        get_credentials = fetcher.get_resolve_func()
        credentials = get_credentials()
        return credentials


class InstanceCredentialsProvider(CredentialsProvider):
    ENV_NAME_FOR_ECS_METADATA = 'ALIBABA_CLOUD_ECS_METADATA'

    def __init__(self):
        self.environ = os.environ

    def load(self):
        if self.ENV_NAME_FOR_ECS_METADATA in self.environ:
            meta_data = self.environ.get(self.ENV_NAME_FOR_ECS_METADATA)
            if meta_data is None:
                raise ClientException(
                    'Credentials',
                    'Environment variable %s cannot be empty.' % self.ENV_NAME_FOR_ECS_METADATA
                )
            role_name = meta_data
            fetcher = InstanceMetadataFetcher(role_name)
            profile = fetcher.get_profile()
            credentials = SecurityCredentials(profile)
            return credentials
        else:
            return None
