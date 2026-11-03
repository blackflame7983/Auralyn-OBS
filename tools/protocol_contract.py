"""Python検証ツールで共有する1.0/1.1のrate契約。正本は本体の仕様・fixture。"""
LEGACY_RATES = (44100, 48000, 96000)
EXTENDED_RATES = (88200, 176400, 192000)
SUPPORTED_RATES = (*LEGACY_RATES, *EXTENDED_RATES)
SINC_MODES = {48000: 'sinc48-poc', 88200: 'sinc88-to48-poc', 96000: 'sinc96-to48-poc',
              176400: 'sinc176-to48-poc', 192000: 'sinc192-to48-poc'}
QUALITY_RATES = tuple(sorted(SINC_MODES))


def stream_minor(rate):
    if rate in LEGACY_RATES:
        return 0
    if rate in EXTENDED_RATES:
        return 1
    raise ValueError('このrateはOBS PCM 1.1の検証対象外です')


def valid_stream(minor, rate):
    return (type(minor) is int and type(rate) is int
            and (minor == 0 and rate in LEGACY_RATES or minor == 1 and rate in SUPPORTED_RATES))
