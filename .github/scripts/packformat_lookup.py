def packformat_lookup(input_version, lookup_path):            #lookup_path should be somewhere like .github/lookup.txt
    with open(lookup_path, 'r') as f:                         #with repo license at top i.e. license: GPL-3.0-or-later
        lines = f.read().split('\n')                          #and lines of mc version with correspoding pack format number
                                                              #i.e. 26.2, 88                 
    for line in lines:
        trimmed = line.strip()                                #clean up hidden text formatting
        if not trimmed or trimmed.startswith('license:'):     #skip empty lines and license declaration
            continue

        lookup_version, packformat_str = [s.strip() for s in trimmed.split(',')]  #split each line into mc version and packformat number
        if lookup_version == input_version:                                       #check requested version against lookup table
            return {'packformat': int(packformat_str)}                            #packformat needs to be converted from string to integer

    if version == '1.0-1.5.x':
        return {'packformat': None}  #no pack format number for pre-1.6

    raise ValueError(f'No valid lookup entry for "{input_version}"')  #error for typos/invalid version sequence in request