#!/usr/bin/env python
import re
import os
import sys
import json
import time
import math
import xxhash

"""
The purpose of this script is to recursively analyze a set of folders containing files, checking against a
JSON catalog for the current file names, paths, and sizes.  Any files in the folder or subfolders that are
not found in the list (or whose name or size changed) will be hashed to check for a renamed match.  It will
then output a list of "changed + matched" and "unmatched" files for the user to examine further.
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
for hash, entry in catalog.items():
    fp = os.path.join(entry['p'],entry['f'])
    catalog_index[fp] = hash

new_data = []
renamed = []
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

# ~200MB takes less than a second
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
            continue
        fullpath = os.path.join(root, file)
        size = os.path.getsize(fullpath)
        if fullpath in catalog_index:
            entry = catalog[catalog_index[fullpath]]
            if size == entry['s']:  # it's a match (or at least close enough for our purposes), so we don't care about this file.
                continue
        
        if len(file) > 40:
            filestr = file[:31] + "..." + file[-6:]
        else:
            filestr = file
        outline = f'{math.floor(filenum/total_num_files * 100)}% done - ' + \
            f'File #{filenum}  ({round(size/1048576,2)} MB)  {filestr}'.ljust(70)
        print(outline, end="\r", flush=True)
        filenum += 1
        hash = hash_file(fullpath)
        item = { 'h': hash, 'f': file, 'p': root, 's': size }
        if hash in catalog:
            renamed.append(item)
        else:
            new_data.append(item)

end = time.time()
delta = end - start
print(f'Time elapsed: {round(delta,2)} seconds.')

print("\n\nItems in the catalog that have been renamed (indented is original from catalog):")
for item in renamed:
    fp = os.path.join(item['p'],item['f'])
    cat_item = catalog[item['h']]
    cat_fp = os.path.join(cat_item['p'],cat_item['f'])
    deleted = "    "
    if not os.path.exists(cat_fp):
        deleted = "DEL "
    print("    " + fp)
    print(f'{deleted}{cat_fp}')
    print("")

print("\n\nItems new to the catalog:")
for item in new_data:
    print(item)

choice = input(f'Do you want to save this information to "renamed.db" and "new.db"? (Y/N) ').lower()
if choice[0] == 'y':
    with open("renamed.db", 'w') as rdbfile:
        json.dump(renamed, rdbfile)
    with open("new.db", 'w') as ndbfile:
        json.dump(new_data, ndbfile)
    print("Wrote db files in the current folder.")

