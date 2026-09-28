"""idles_generated.py — AUTHORED BY ASTRA, bound and gated by idle_import.py.

⚠ DO NOT HAND-EDIT. Regenerate from the JSON so the loop and binding checks run.
  Every asset name here was a SEMANTIC TARGET in Astra's output — it never saw the
  disk — and was bound to a real file by idle_import, which refuses anything that
  does not bind rather than substituting a default face.
"""

IDLES = {
    'quiet_breath': [
        ({}, 4, 0, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {'belly': 1.0}),
        ({}, 4, 18, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {'belly': 1.03}),
        ({}, 10, 24, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {'belly': 1.0}),
    ],
    'content_pendulum': [
        ({'body': 0, 'head': 0}, 4, 0, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {}),
        ({'body': 4, 'head': -2}, 4, 12, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {}),
        ({'body': -4, 'head': 2}, 4, 24, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {}),
        ({'body': 0, 'head': 0}, 4, 12, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {}),
    ],
    'little_flipper_stretch': [
        ({'flipper_L': 0, 'flipper_R': 0}, 4, 0, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {}),
        ({'flipper_L': -12, 'flipper_R': 12}, 14, 16, 'ease_in_out', {'visor': 'squint', 'beak': 'closed'}, {}),
        ({'flipper_L': 0, 'flipper_R': 0}, 10, 18, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {}),
    ],
    'flipper_double_flick': [
        ({'flipper_R': 0}, 6, 0, 'ease_out', {'visor': 'content', 'beak': 'closed'}, {}),
        ({'flipper_R': 10}, 2, 3, 'ease_out', {'visor': 'content', 'beak': 'closed'}, {}),
        ({'flipper_R': 0}, 3, 4, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {}),
        ({'flipper_R': 7}, 2, 3, 'ease_out', {'visor': 'content', 'beak': 'closed'}, {}),
        ({'flipper_R': 0}, 13, 4, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {}),
    ],
    'small_yawn': [
        ({}, 8, 0, 'linear', {'visor': 'content', 'beak': 'closed'}, {}),
        ({}, 3, 0, 'linear', {'visor': 'content', 'beak': 'open'}, {}),
        ({}, 18, 0, 'linear', {'visor': 'squint', 'beak': 'wide'}, {}),
        ({}, 4, 0, 'linear', {'visor': 'squint', 'beak': 'open'}, {}),
        ({}, 3, 0, 'linear', {'visor': 'squint', 'beak': 'closed'}, {}),
        ({}, 14, 0, 'linear', {'visor': 'content', 'beak': 'closed'}, {}),
    ],
    'imagined_notification': [
        ({'body': 0, 'head': 0}, 8, 0, 'linear', {'visor': 'content', 'beak': 'closed'}, {}),
        ({'body': 0, 'head': 0}, 2, 0, 'linear', {'visor': 'wide', 'beak': 'closed'}, {}),
        ({'body': -4, 'head': -2}, 4, 3, 'ease_out', {'visor': 'wide', 'beak': 'open'}, {}),
        ({'body': 1.5, 'head': 0}, 2, 7, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {}),
        ({'body': 0, 'head': 0}, 12, 12, 'ease_in_out', {'visor': 'content', 'beak': 'closed'}, {}),
    ],
}
