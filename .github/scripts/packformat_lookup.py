def packformat_lookup(input_version, lookup_path): #lookup_path should be somewhere like .github/lookup.txt
    with open(lookup_path, 'r') as f:              #with repo license at top i.e. license: GPL-3.0-or-later
        lines = f.read().split('\n')               #and lines of mc version with correspoding pack format number
                                                   #i.e. 26.2, 88                 
    for line in lines:
        trimmed = line.strip()
        if not trimmed or trimmed.startswith('license:'):
            continue

        lookup_version, packformat_str = [s.strip() for s in trimmed.split(',')]
        if lookup_version == input_version:
            return {'format': int(packformat_str)}

    if version == '1.0-1.5.x':
        return {'format': None}  # pre-1.6 era, no format number exists

    raise ValueError(f'No lookup entry found for version "{version}"')