#!/usr/bin/env python
import re
import os
import sys
import json
import time
import math
import xxhash

"""
The purpose of this script is to recursively analyze a set of folders containing files,
updating a JSON catalog with the current file names, paths, and sizes for the matching
hash values.  Any hash values that were not found (such as deleted files) will not have
their entries affected, and any new files will not be added.

Once the catalog values are updated, that serves to protect existing files from being
removed as duplicates simply because of being moved.

Run this script on already cataloged files that have been renamed or moved for organization.

Do not run this script on new files that may contain copies of previously deleted unwanted
files, since it will simply update the catalog to point to the new unwanted files.

The script will also flag any duplicates of catalog items that it finds as well, although
it will not automatically resolve them.
"""

start = time.time()

db_path = os.getenv('CATALOG_DB_PATH', os.path.expanduser('~'))
catalog_path = os.path.join(db_path, 'catalog.db')
ignore_extensions = ('.db', '.bak')

catalog = {}
if os.path.exists(catalog_path):
    print("Loading catalog...")
    with open(catalog_path, 'r') as catalogfile:
        catalog = json.load(catalogfile)
"""
Each catalog entry is a dictionary entry that looks like this:
"2a6ff19bd0": {        # hash
   "f":"file.txt",     # filename
   "p":"/home/user",   # path
   "s":"12345"         # size in bytes
}
"""

"""
Make a new dictionary derived from catalog that looks like this:
"/home/user/file.txt": {    # full path
   "h":"2a6ff19bd0"         # hash
}
This makes it much quicker to look up filenames and get the hash and catalog entry for them.
"""
catalog_index = {}
catalog_list = []   # uses format of new_data below. Easier to search through by size, filename.
for hash, entry in catalog.items():
    fp = os.path.join(entry['p'],entry['f'])
    catalog_index[fp] = hash
    item = { 'h': hash, 'f': entry['f'], 'p': entry['p'], 's': entry['s'] }
    catalog_list.append(item)

new_data = []   
"""
A slightly different data type from the catalog, since the new_data list _has_ to be able to have duplicates in it,
but the catalog can't.
Each list item is an object that looks like this:
{
   "h":"2a6ff19bd0"    # hash
   "f":"file.txt",     # filename
   "p":"/home/user",   # path
   "s":"12345"         # size in bytes
}
"""

def FindHashByFileAndSize(file, path, size):
    global catalog_list
    subset = [i for i in catalog_list if i['s'] == size]  # get all with the same size
    if len(subset) == 1:
        return subset[0]['h']  # remember, we're assuming that all these files are already cataloged.
    # there is more than 1 file with that size, so narrow it to files that don't exist any more
    ss2 = [i for i in subset if not os.path.exists(os.path.join(i['p'],i['f']))]
    if len(ss2) == 1:
        return ss2[0]['h']  # return the hash of the file that's not there anymore.
    return ""    # can't determine the hash, so go ahead and hash the file again.


def hash_file(fpath, blocksize=65536, hasher='xx64'):
    if hasher == 'xx32':
        hasher = xxhash.xxh32()
    elif hasher == 'xx64':
        hasher = xxhash.xxh64()

    with open(fpath, 'rb') as file:
        buf = file.read(blocksize)
        # otherwise hash the entire file
        while len(buf) > 0:
            hasher.update(buf)
            buf = file.read(blocksize)
    # Get the hashed representation
    text = hasher.hexdigest()
    return text


starting_path = os.path.abspath(u".")
total_num_files = sum([len(files) for r, d, files in os.walk(starting_path)])

# collect all the info for filename, path, size, and hash
# traverse the current directory, and list all files (full path)
print(f'Analyzing {total_num_files} files...')
filenum = 0
for root, dirs, files in os.walk(starting_path):
    for file in files:
        if file.endswith(ignore_extensions):
            filenum += 1
            continue
        fullpath = os.path.join(root, file)
        size = os.path.getsize(fullpath)
        # don't hash EVERYTHING, since that takes a long time.  If filename and size matches, call it good.
        if fullpath in catalog_index:
            entry = catalog[catalog_index[fullpath]]
            if size == entry['s']:  # it's a match (or at least close enough for our purposes), so we don't care about this file.
                filenum += 1
                continue
        
        if len(file) > 40:
            filestr = file[:31] + "..." + file[-6:]
        else:
            filestr = file
        outline = f'{math.floor(filenum/total_num_files * 100)}% done - ' + \
            f'File #{filenum}  ({round(size/1048576,2)} MB)  {filestr}'.ljust(70)
        print(outline, end="\r", flush=True)
        filenum += 1
        # we couldn't find the full path already in the catalog, so we need to try to deduce the hash
        hash = FindHashByFileAndSize(file, root, size)
        if hash == "":   # couldn't deduce the hash, so recalculate it
            hash = hash_file(fullpath)
        item = { 'h': hash, 'f': file, 'p': root, 's': size }
        if hash in catalog:
            if catalog[hash] == item:  # same exact hash, size, and location is already in the catalog, so don't re-add
               continue
        new_data.append(item)

# identify duplicates
duplicates_found = []
print("\n\rChecking for duplicates for catalog entries...")
for file in new_data:
    hash = file['h']
    if hash not in catalog:  # ignore non-catalog duplicates, since we're only concerned with updating the catalog.
        continue
    fullpath = os.path.join(file['p'], file['f'])
    # find the duplicates within the new data
    matching = [i for i in new_data if i['h'] == hash and os.path.join(i['p'], i['f']) not in duplicates_found]
    if len(matching) < 2:  # there will always be 1 match here
        continue
    print("Duplicates found:")
    for m in matching:
        fp = os.path.join(m["p"], m["f"])
        print(f'  {fp}')
        duplicates_found.append(fp)

end = time.time()
delta = end - start
print(f'Time elapsed: {round(delta,2)} seconds.')


# update the catalog except for those files that are duplicates.
upd_files = 0
for i in new_data:
    if os.path.join(i['p'], i['f']) not in duplicates_found:
        hash = i["h"]
        if hash in catalog:  # we are only updating, not adding.
            catalog[hash] = { 'f': i['f'], 'p': i['p'], 's': i['s'] }
            upd_files += 1
print(f'Updated {upd_files} files in the catalog.')
with open(catalog_path, 'w') as catalogfile:
   json.dump(catalog, catalogfile)

