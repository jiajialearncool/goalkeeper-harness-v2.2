"""User-selected model configuration shared by entry adapters and runtime."""
from ..models import MODEL
from .provider_catalog import ACCOUNT_MODELS, CATALOG, is_account_provider

ACCOUNT_EFFORTS = [('auto', '自动'), ('low', '低'), ('medium', '中'), ('high', '高'), ('xhigh', '极高')]


def selection(settings, provider, model):
    saved = settings.get('model_selections', {}).get(provider, {})
    default = ('auto' if is_account_provider(provider)
               else CATALOG.get(provider, {}).get('default_effort', 'none'))
    return {'provider': provider, 'model': model,
            'reasoningEffort': saved.get('effort', default) if saved.get('model', model) == model else default}


def validate_selection(provider, model, effort, profiles):
    if is_account_provider(provider):
        if model not in ACCOUNT_MODELS:
            raise ValueError('请选择账号列表中的模型。')
        if effort not in dict(ACCOUNT_EFFORTS):
            raise ValueError('请选择自动、低、中、高或极高推理等级。')
    else:
        if model != profiles.get(provider, {}).get('model'):
            raise ValueError('请选择已配置的模型。')
        if effort not in CATALOG.get(provider, {}).get('efforts', ['none']):
            raise ValueError('请选择此模型支持的推理等级。')
    return {'model': model, 'effort': effort}


def directory(settings, profiles, accounts=None, primary_connected=True):
    def model_entry(model, efforts):
        return {'id': model, 'name': model, 'reasoning': {
            'defaultEffort': efforts[0][0], 'efforts': [{'id': k, 'name': v} for k, v in efforts]}}
    groups = ([{'id': 'account', 'name': 'GoalKeeper 账号',
                'models': [model_entry(model, ACCOUNT_EFFORTS) for model in ACCOUNT_MODELS]}]
              if primary_connected else [])
    groups += [{'id': item['id'], 'name': item['name'],
                'models': [model_entry(model, ACCOUNT_EFFORTS) for model in item['models']]}
               for item in (accounts or []) if item.get('connected')]
    effort_names = {'none': '默认', 'low': '低', 'medium': '中', 'high': '高', 'xhigh': '极高'}
    groups += [{'id': p['id'], 'name': p['name'], 'models': [model_entry(
        p['model'], [(effort, effort_names[effort]) for effort in CATALOG[p['id']].get('efforts', ['none'])]
    )]}
               for p in profiles if p['configured']]
    provider = settings.get('model_provider', 'account')
    if is_account_provider(provider):
        model = settings.get('model_selections', {}).get(provider, {}).get('model', MODEL)
    else:
        model = settings.get('model_profiles', {}).get(provider, {}).get('model', '')
    return {'current': selection(settings, provider, model), 'groups': groups,
            'routable': any(g['id'] == provider for g in groups), 'status': 'ready',
            'failures': [], 'pending': None, 'error': None}
