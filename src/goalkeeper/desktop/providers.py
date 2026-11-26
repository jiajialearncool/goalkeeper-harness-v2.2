"""Desktop-only API providers. Secrets never enter settings, events or request archives."""
from copy import deepcopy

import httpx
import keyring


from .provider_catalog import ACCOUNT_CATALOG, CATALOG

from ..runtime.providers import ChatAPIProvider, messages_for  # noqa: F401
from ..runtime.model_selection import ACCOUNT_EFFORTS, directory, selection, validate_selection
from ..runtime.provider_catalog import is_account_provider
from ..models import MODEL
KEYCHAIN_SERVICE = 'GoalKeeper Desktop Models'


class ModelSettings:
    def __init__(self, manager):
        self.manager = manager

    def key_account(self, provider):
        if provider not in CATALOG:
            raise ValueError('不支持此模型提供商。')
        return self.manager.account_key + ':' + provider

    def secret(self, provider):
        return keyring.get_password(KEYCHAIN_SERVICE, self.key_account(provider))

    def account_connected(self, provider):
        if provider == 'account':
            # Offline tests can inject a provider without installing credentials.
            if self.manager.injected_provider:
                return True
            broker = self.manager.auth
        else:
            broker = self.manager.account_auths.get(provider)
        return bool(broker and broker.status().get('connected'))

    def reconcile_account_provider(self):
        """Move a stale account selection to an account that can actually run."""
        settings = self.manager.index['settings']
        selected = settings.get('model_provider', 'account')
        if not is_account_provider(selected) or self.account_connected(selected):
            return selected
        fallback = next((identifier for identifier in ACCOUNT_CATALOG
                         if self.account_connected(identifier)), None)
        if fallback is None:
            return selected
        prior = settings.get('model_selections', {}).get(selected, {})
        target = settings.setdefault('model_selections', {}).get(fallback)
        if target is None:
            model = prior.get('model', MODEL)
            effort = prior.get('effort', 'auto')
            settings['model_selections'][fallback] = validate_selection(
                fallback, model if model in ACCOUNT_CATALOG[fallback]['models'] else MODEL,
                effort if effort in dict(ACCOUNT_EFFORTS) else 'auto', {})
        settings['model_provider'] = fallback
        self.manager.persist()
        return fallback

    def ensure_account_connected(self, provider):
        if is_account_provider(provider) and not self.account_connected(provider):
            raise ValueError('当前选择的账号未连接，请在设置 → 模型中选择已连接账号。')

    def rebind_config(self, config):
        if not is_account_provider(config.provider_id) or self.account_connected(config.provider_id):
            return config
        provider = self.reconcile_account_provider()
        if not is_account_provider(provider):
            raise ValueError('此前执行使用的账号已断开，请选择一个已连接账号后重新发送要求。')
        self.ensure_account_connected(provider)
        return config.model_copy(update={'provider_id': provider})

    def status(self):
        self.reconcile_account_provider()
        settings = self.manager.index['settings']
        profiles = settings.get('model_profiles', {})
        profiles = [{'id': p, 'name': CATALOG[p]['name'], **v,
                     'configured': bool(self.secret(p))} for p, v in profiles.items() if p in CATALOG]
        accounts = self.account_profiles()
        return {'catalog': [{'id': k, **v} for k, v in CATALOG.items()],
                'accountCatalog': [{'id': k, **v} for k, v in ACCOUNT_CATALOG.items()],
                'accounts': accounts,
                'selected': settings.get('model_provider', 'account'),
                'profiles': profiles, 'directory': directory(
                    settings, profiles, accounts, primary_connected=self.account_connected('account'))}

    def account_profiles(self):
        values = []
        for identifier, definition in ACCOUNT_CATALOG.items():
            broker = self.manager.account_auths.get(identifier)
            status = broker.status() if broker else {'connected': False}
            values.append({'id': identifier, 'name': definition['name'], 'models': definition['models'],
                           'connected': bool(status.get('connected')), 'email': status.get('email'),
                           'error': status.get('error')})
        return values

    def idle(self):
        if self.manager.jobs:
            raise ValueError('请先停止正在执行的任务，再修改模型配置。')

    def save(self, provider, model, key=None):
        self.idle()
        account = self.key_account(provider)
        if model not in CATALOG[provider]['models']:
            raise ValueError('请选择列表中的模型。')
        if key is not None and (not isinstance(key, str) or len(key) > 4096 or any(c.isspace() for c in key)):
            raise ValueError('API 密钥格式无效。')
        prior_key = self.secret(provider)
        if not key and not prior_key:
            raise ValueError('请填写该提供商的 API 密钥。')
        before = deepcopy(self.manager.index['settings'])
        try:
            if key:
                keyring.set_password(KEYCHAIN_SERVICE, account, key)
            self.manager.index['settings'].setdefault('model_profiles', {})[provider] = {
                'model': model, 'connection': 'untested'}
            self.manager.persist()
        except Exception:
            self.manager.index['settings'] = before
            if key:
                if prior_key:
                    keyring.set_password(KEYCHAIN_SERVICE, account, prior_key)
                else:
                    keyring.delete_password(KEYCHAIN_SERVICE, account)
            raise
        return self.status()

    def select(self, provider, model=None, effort=None):
        self.idle()
        if is_account_provider(provider):
            self.ensure_account_connected(provider)
        elif not is_account_provider(provider):
            self.key_account(provider)
            if provider not in self.manager.index['settings'].get('model_profiles', {}) or not self.secret(provider):
                raise ValueError('请先保存该提供商的 API 密钥和模型。')
        before = deepcopy(self.manager.index['settings'])
        settings = self.manager.index['settings']
        expected = (settings.get('model_selections', {}).get(provider, {}).get('model', MODEL)
                    if is_account_provider(provider) else settings['model_profiles'][provider]['model'])
        model = expected if model is None else model
        if effort is None:
            effort = selection(settings, provider, model)['reasoningEffort']
        picked = validate_selection(provider, model, effort, settings.get('model_profiles', {}))
        self.manager.index['settings']['model_provider'] = provider
        settings.setdefault('model_selections', {})[provider] = picked
        try:
            self.manager.persist()
        except Exception:
            self.manager.index['settings'] = before
            raise
        return self.status()

    def delete_account(self, provider):
        self.idle()
        if provider not in ACCOUNT_CATALOG:
            raise ValueError('不支持此第三方账号。')
        broker = self.manager.account_auths.get(provider)
        if broker:
            broker.logout()
        settings = self.manager.index['settings']
        settings.get('model_selections', {}).pop(provider, None)
        if settings.get('model_provider') == provider:
            settings['model_provider'] = 'account'
        self.manager.persist()
        return self.status()

    def delete(self, provider):
        self.idle()
        account = self.key_account(provider)
        before = deepcopy(self.manager.index['settings'])
        prior_key = self.secret(provider)
        try:
            if prior_key:
                keyring.delete_password(KEYCHAIN_SERVICE, account)
            self.manager.index['settings'].get('model_profiles', {}).pop(provider, None)
            self.manager.index['settings'].get('model_selections', {}).pop(provider, None)
            if self.manager.index['settings'].get('model_provider') == provider:
                self.manager.index['settings']['model_provider'] = 'account'
            self.manager.persist()
        except Exception:
            self.manager.index['settings'] = before
            if prior_key:
                keyring.set_password(KEYCHAIN_SERVICE, account, prior_key)
            raise
        return self.status()

    async def verify(self, provider, http=None):
        self.idle()
        key = self.secret(provider)
        profile = self.manager.index['settings'].get('model_profiles', {}).get(provider)
        if not key or not profile:
            raise ValueError('请先保存配置。')
        client = http or httpx.AsyncClient(timeout=20, follow_redirects=False)
        try:
            response = await client.get(CATALOG[provider]['base_url'] + '/models',
                                        headers=({'x-api-key': key, 'anthropic-version': '2023-06-01'}
                                                 if CATALOG[provider].get('protocol') == 'anthropic'
                                                 else {'Authorization': 'Bearer ' + key}))
            if response.status_code != 200:
                profile['connection'] = 'failed'
                self.manager.persist()
                raise ValueError(f'模型连接检查失败：HTTP {response.status_code}。请检查密钥、权限或服务状态。')
            ids = {row.get('id') for row in response.json().get('data', []) if isinstance(row, dict)}
            if profile['model'] not in ids:
                profile['connection'] = 'failed'
                self.manager.persist()
                raise ValueError('连接已建立，但该账号的模型列表中没有所选模型。')
            profile['connection'] = 'connected'
            self.manager.persist()
            return self.status()
        except httpx.HTTPError as error:
            profile['connection'] = 'failed'
            self.manager.persist()
            raise ValueError('模型连接检查失败：网络连接异常。') from error
        finally:
            if http is None:
                await client.aclose()


