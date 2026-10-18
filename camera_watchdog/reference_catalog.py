"""Shared day/night slots. Existing filenames and IDs remain compatible."""
DAY_REFERENCES = ('day', 'day_2', 'day_3', 'day_4', 'day_5')
NIGHT_REFERENCES = ('night', 'night_alt', 'night_3', 'night_4', 'night_5')
REFERENCE_GROUPS = {'day': DAY_REFERENCES, 'night': NIGHT_REFERENCES}
REFERENCE_KINDS = DAY_REFERENCES + NIGHT_REFERENCES
REFERENCE_FILES = {kind: 'reference_' + kind + '.jpg' for kind in REFERENCE_KINDS}


def reference_label(kind):
    for group, kinds in REFERENCE_GROUPS.items():
        if kind in kinds:
            return ('Día' if group == 'day' else 'Noche') + ' ' + str(kinds.index(kind) + 1)
    return 'Escena anterior'


def saved_reference_paths(directory):
    return {kind: directory / name for kind, name in REFERENCE_FILES.items()
            if (directory / name).is_file()}
