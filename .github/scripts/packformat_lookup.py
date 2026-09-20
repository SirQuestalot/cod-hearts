def packformat_lookup(input_version, lookup_path):            #included lookup_path should be somewhere like .github/lookup.txt
    with open(lookup_path, 'r') as f:                         #with repo license at top i.e. license: GPL-3.0-or-later
        lines = f.read().split('\n')                          #and lines of mc version with correspoding pack format number i.e. 26.2, 88
    
    for line_number, line in enumerate(lines, start=1):       #keep track of line number for each line
        trimmed = line.strip()                                #clean up hidden text formatting
        if not trimmed or trimmed.startswith('license:'):     #skip empty lines and license declaration
            continue

        parts = [s.strip() for s in trimmed.split(',')]                                     #split each line into mc version and packformat number
        if len(parts) != 2:                                                                 #each line should only have two values, mc version and pack format number i.e. 26.2, 88
            raise ValueError(f'Invalid line "{line_number} {trimmed}" in lookup file')      #throw error with line# and line content for typos/incorrect data entry in lookup_path
        lookup_version, packformat_str = parts

        if lookup_version == input_version:                 #validate requested version against lookup table
            return {'packformat': int(packformat_str)}      #packformat needs to be converted from string to integer

    if input_version == '1.0-1.5.x':
        return {'packformat': None}     #no pack format number for pre-1.6

    raise ValueError(f'No matching lookup entry for "{input_version}"')     #throw error for typos/invalid version sequence in input_version request