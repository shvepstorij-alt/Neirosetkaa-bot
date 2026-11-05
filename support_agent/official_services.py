"""Curated public product identities, never domains supplied by a customer.

Only website identity is recorded here. Plan names, features, prices and
availability still require current cited research. Checked 05.11.2026 using
public first-party pages. Redirects are recorded explicitly, not inferred.
"""
import re
from urllib.parse import urlsplit

# id: (public search label, allowed first-party domains, primary proof URLs)
_SERVICES={
 'zoom':('Zoom Workplace video meetings subscription',('zoom.com','zoom.us'),('https://www.zoom.com/en/products/collaboration-tools/',)),
 'krea':('Krea AI creative tools subscription',('krea.ai',),('https://www.krea.ai/pricing',)),
 'chatgpt':('ChatGPT consumer subscription',('openai.com','chatgpt.com'),('https://openai.com/chatgpt/pricing/','https://chatgpt.com/pricing/')),
 'codex':('OpenAI Codex coding agent capabilities and ChatGPT plan access not API billing',('developers.openai.com','learn.chatgpt.com','openai.com','chatgpt.com'),('https://developers.openai.com/api/docs/guides/code-generation',)),
 'claude':('Claude consumer subscription',('anthropic.com','claude.com','claude.ai'),('https://claude.com/pricing',)),
 'perplexity':('Perplexity consumer subscription',('perplexity.ai',),('https://www.perplexity.ai/help-center/en/articles/11187416-which-perplexity-subscription-plan-is-right-for-you',)),
 'gemini':('Google Gemini consumer subscription',('gemini.google','gemini.google.com','support.google.com','one.google.com','blog.google','ai.google.dev'),('https://gemini.google/subscriptions/',)),
 'canva':('Canva design editor consumer subscription',('canva.com',),('https://www.canva.com/pricing/',)),
 'capcut':('CapCut video editor consumer subscription',('capcut.com',),('https://www.capcut.com/',)),
 'cursor':('Cursor coding editor consumer subscription',('cursor.com',),('https://cursor.com/pricing',)),
 'telegram_premium':('Telegram Premium consumer subscription',('telegram.org',),('https://telegram.org/faq_premium',)),
 'github':('GitHub Copilot consumer coding subscription',('github.com','docs.github.com','github.blog'),('https://github.com/features/copilot/plans',)),
 'genspark':('Genspark AI Workspace consumer subscription',('genspark.ai',),('https://www.genspark.ai/',)),
 'supergrok':('Grok SuperGrok consumer subscription',('grok.com','x.ai'),('https://grok.com/',)),
 'zai':('Z.ai GLM consumer subscription',('z.ai',),('https://z.ai/','https://z.ai/pricing')),
 'elevenlabs':('ElevenLabs creative voice consumer subscription not API',('elevenlabs.io',),('https://elevenlabs.io/pricing',)),
 'heygen':('HeyGen video avatar consumer subscription',('heygen.com',),('https://www.heygen.com/pricing',)),
 'kimi':('Kimi membership consumer subscription',('kimi.com',),('https://www.kimi.com/en/help/membership/membership-overview',)),
 'kling':('Kling AI image video consumer subscription',('klingai.com',),('https://app.klingai.com/llms.txt',)),
 'lovable':('Lovable app builder consumer subscription',('lovable.dev',),('https://lovable.dev/pricing',)),
 'make':('Make workflow automation consumer subscription',('make.com',),('https://www.make.com/pricing',)),
 'manus':('Manus AI agent consumer subscription',('manus.im',),('https://manus.im/',)),
 'runway':('Runway creative image video consumer subscription not API',('runway.com','runwayml.com'),('https://runwayml.com/pricing','https://runway.com/pricing','https://help.runwayml.com/hc/en-us')),
 'midjourney':('Midjourney image video consumer subscription',('midjourney.com',),('https://docs.midjourney.com/hc/en-us',)),
 'higgsfield':('Higgsfield AI image video consumer subscription not API',('higgsfield.ai',),('https://higgsfield.ai/pricing',)),
 'suno':('Suno music creative consumer subscription',('suno.com',),('https://suno.com/pricing',)),
 'gamma':('Gamma presentations consumer subscription',('gamma.app',),('https://gamma.app/pricing',)),
 'freepik':('Freepik Magnific creative suite consumer subscription',('freepik.com','magnific.com'),('https://www.freepik.com/pricing','https://www.magnific.com/pricing')),
 'spotify':('Spotify Premium consumer music subscription',('spotify.com',),('https://www.spotify.com/premium/',)),
 'youtube':('YouTube Premium consumer subscription',('youtube.com','support.google.com'),('https://www.youtube.com/premium','https://support.google.com/youtube/answer/6308116')),
 'openrouter':('OpenRouter model API credit billing not ChatGPT consumer subscription',('openrouter.ai',),('https://openrouter.ai/',)),
 'notion':('Notion workspace consumer subscription',('notion.com',),('https://www.notion.com/pricing',)),
 'motion':('Motion scheduling productivity consumer subscription',('usemotion.com',),('https://www.usemotion.com/pricing',)),
 'appstore':('Apple Gift Card Apple Account App Store iCloud consumer help',('apple.com',),('https://support.apple.com/en-us/118245',)),
}

EXTRA_ALIASES={
 'grok':'supergrok','super grok':'supergrok','telegram premium':'telegram_premium',
 'runway gen-4':'runway','runway gen 4':'runway','lovable pro':'lovable',
 'canva pro':'canva','perplexity pro':'perplexity','kling ai':'kling',
 'higgsfield ai':'higgsfield','z ai':'zai','icloud':'appstore','icloud appstore':'appstore',
 'youtube premium':'youtube','github copilot':'github','open router':'openrouter',
}


def canonical_product(value):
    if not isinstance(value,str):return value
    text=re.sub(r'\s+',' ',value.strip().casefold())
    if text in _SERVICES:return text
    if text in EXTRA_ALIASES:return EXTRA_ALIASES[text]
    # Only whole registered aliases are identities. A brand mentioned inside an
    # arbitrary sentence does not authorize a different domain filter.
    from .catalog_context import ALIASES,_words
    words=_words(text)
    found={key for key,aliases in ALIASES.items() if key in _SERVICES
           and any(_words(alias)==words for alias in aliases)}
    return next(iter(found)) if len(found)==1 else value


def registry():
    return {key:(label,domains) for key,(label,domains,proof) in _SERVICES.items()}


def identity_metadata(product):
    key=canonical_product(product)
    if key not in _SERVICES:return None
    label,domains,proof=_SERVICES[key]
    return {'id':key,'verification':'curated_first_party_identity','checked_at':'2026-11-05',
            'domains':list(domains),'source_urls':list(proof),
            'scope':'website identity only; features and prices are not cached here'}


def valid_identity_source(url,domains):
    try:
        p=urlsplit(url)
        return (p.scheme=='https' and not p.username and not p.password and p.port in (None,443)
                and bool(p.hostname) and any(p.hostname==d or p.hostname.endswith('.'+d) for d in domains))
    except (ValueError,TypeError):return False


# Fail closed on a bad registry edit rather than silently broadening trust.
for _key,(_label,_domains,_proof) in _SERVICES.items():
    if not _domains or not _proof or not all(valid_identity_source(u,_domains) for u in _proof):
        raise ValueError('Invalid curated public identity: '+_key)

def allowed_source(product,url):
    """On shared official platforms, a user's page is not provider policy."""
    key=canonical_product(product)
    p=urlsplit(url);host=(p.hostname or '').removeprefix('www.');path=p.path or '/'
    if key=='github' and host=='github.com':
        return path=='/pricing' or path=='/pricing/' or path.startswith(('/features/','/enterprise/','/security/','/resources/','/readme/'))
    if key=='youtube':
        if host=='support.google.com':return path.startswith('/youtube/')
        if host=='youtube.com':return path.startswith(('/premium','/youtube_premium','/paid_memberships','/howyoutubeworks/','/t/'))
        return False
    return True
