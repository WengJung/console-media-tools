#!/usr/bin/env python
"""
The purpose of this script is to recursively analyze a set of folders containing files,
* identify which files are duplicates
* remove the duplicates
* create (or add to) a JSON catalog of all the file names, paths, sizes, and hashes.

Existing catalog items will be used to determine whether the analyzed files should be added
to the catalog or discarded. Do not run the script on already cataloged files unless they
are completely unchanged (i.e. not renamed).  It will attempt to delete them if the path and
filename do not perfectly match the catalog.  (See also the update_catalog_locations.py script.)

The deletions only come at the end of the script, after interacting with the user.
"""

import re
import os
import termios
import fcntl
import sys, tty
import readline
import json
import time
import math
import xxhash
from unidecode import unidecode


start = time.time()

# Get commandline parameters
# Ignore-catalog-dupes is for when we definitely don't want to remove any files that are already in the catalog.
remove_catalog_dupes = True
if len(sys.argv) > 1:
    if "ignore-catalog-dupes" in sys.argv:
        print("Ignoring catalog duplicates.")
        remove_catalog_dupes = False

db_path = os.getenv('CATALOG_DB_PATH', os.path.expanduser('~'))
catalog_path = os.path.join(db_path, 'catalog.db')
wip_db_path = os.path.join(db_path, 'wip.db')

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
if os.path.exists(wip_db_path):
    print("Loading work-in-progress...")
    with open(wip_db_path, 'r') as wipfile:
        new_data = json.load(wipfile)
last_saved = time.time()   # keeps track of when we last saved the work in progress

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

duplicates = []
"""
The duplicates list keeps track of which files are duplicates, so the user can decide which to keep.
Each list item is a list of objects that looks like this:
{
   "fp":"/home/user/file.txt",  # full path
   "cat": true                  # whether this item is already in the catalog
}
"""

""" "Dupe Rules" (Rules for dealing with duplicates) go here
"""
dr_favored_paths = []  # more specific paths take precedent.

to_be_removed = []   # list of full paths of files to be removed at the end of the script

# ------  Screen-related stuff
SCREEN_X = os.get_terminal_size().columns
SCREEN_Y = os.get_terminal_size().lines
HEADER_SIZE = 5  # number of rows in header
MAX_FNAME_LENGTH = SCREEN_X - 6  # max displayed length of filename before shortening
NUM_FILES_PER_PAGE = SCREEN_Y - (HEADER_SIZE + 1)

current_position = 0  # which file we are on

CLEAR_SCREEN = "\033[2J"
NORMAL_TEXT = "\033[97;40m"
HIGHLIGHT_TEXT = "\033[30;47m"
STATUS_TEXT = HIGHLIGHT_TEXT


def cprint(str, color="\033[97;44m", newline = True):
    if not newline:
        print(color + str + NORMAL_TEXT, flush = True, end='')
    else:
        print(color + str + NORMAL_TEXT, flush = True)


def MoveCursor(x, y):
    print(f"\033[{y};{x}H", end = "")


def ShowCursor(b_show = True):
    if b_show:
        print("\033[?25h", end = "")
    else:
        print("\033[?25l", end = "")


def string_n(ch, mult):
    return ''.join([ch for x in range(1,mult)])


def centerprint(str, y, color=''):
    global SCREEN_X
    start_x = math.floor((SCREEN_X - len(str)) / 2)
    MoveCursor(start_x, y)
    if color == '':
        print(str)
    else:
        cprint(str, color)


def ClearLine(y):
    MoveCursor(0, y)
    print("\033[2K", end = '')


# Input functions --------------------------
def getch():
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)  # for one character
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch

def getch_nonblock():  # used to check if there is anything in the buffer
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)  # for one character
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)
        return ch

# Read arrow keys correctly
def getKey():
    firstChar = getch()
    if firstChar == '\x1b':  # escape
        # quickly do two non-blocking reads
        sequence = getch_nonblock() + getch_nonblock()  # get the escape sequence, if any
        if len(sequence) == 0:  # they just pressed escape and nothing else
            return 'esc'
        escaped = {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "[3":"del", "[F":"end", "[H":"home", 
                   "[5":"pgup", "[6":"pgdn"}
        try:
            return escaped[sequence]
        except:
            print(sequence)
            return ''  # didn't recognized it
    else:
        return firstChar


def rlinput(prompt, prefill=''):
    readline.set_startup_hook(lambda: readline.insert_text(prefill))
    try:
        return input(prompt)
    finally:
        readline.set_startup_hook()


# TUI stuff --------------------------------------

def InputBox(title, default):
    global SCREEN_Y, SCREEN_X
    y = math.ceil(SCREEN_Y / 2)
    MoveCursor(0, y - 2)
    print(string_n('=', SCREEN_X))
    ClearLine(y - 1)
    MoveCursor(0, y - 1)
    print(title)
    ClearLine(y)
    ClearLine(y + 1)
    MoveCursor(0, y + 2)
    print(string_n('=', SCREEN_X))
    MoveCursor(0, y)
    ShowCursor(True)
    r = rlinput('', default)
    ShowCursor(False)
    return r


def DrawWindow(title, w, h):  # auto-center this
    global SCREEN_X, SCREEN_Y
    start_x = math.floor((SCREEN_X - w) / 2)
    start_y = math.floor((SCREEN_Y - h) / 2)
    MoveCursor(start_x, start_y)
    if len(title) > w-2:
        title = title[:-(w-5)] + "..."
    print(f'╔{title.ljust(w-2,"═")}╗')
    for y in range(start_y + 1, start_y + h - 1):
        MoveCursor(start_x, y)
        print(f'║{" "*(w-2)}║')
    MoveCursor(start_x, start_y + h - 1)
    print(f'╚{"═"*(w-2)}╝')
    return (start_x, start_y)


def ClearMainArea():
    global HEADER_SIZE
    MoveCursor(1, HEADER_SIZE+1)
    print("\033[J", end = "")  # clears everything below!

    
def SetStatusBar(left="", right=""):
    global SCREEN_X, SCREEN_Y
    string = left + string_n(" ",SCREEN_X - (len(left) + len(right))) + right
    MoveCursor(1,SCREEN_Y)
    cprint(string, STATUS_TEXT, False)
    MoveCursor(1, 1)


def MakeSelection(title, itemlist, WWIDTH, WHEIGHT):
    start_x, start_y = DrawWindow(title, WWIDTH + 2, WHEIGHT + 2)
    current_item = 0
    item_count = len(itemlist)
    first = True
    while True:
        if first:   # kludge to make sure we draw the options first.
            first = False
        else:
            key = getKey().lower()
            if key == "up":
                if current_item > 0:
                    current_item -= 1
            elif key == "down":
                if current_item < item_count - 1:
                    current_item += 1
            elif key == '\r':
                return itemlist[current_item]
            elif key == 'esc':
                return ''
            else:
                continue
        # drawing section
        if WHEIGHT >= item_count:  # all on one page. Easy.
            start_idx = 0
            end_idx = item_count
        else:
            start_idx = int(current_item - math.floor(WHEIGHT / 2))
            if start_idx < 0:
                start_idx = 0
                end_idx = start_idx + WHEIGHT
            else:
                end_idx = int(current_item + math.ceil(WHEIGHT / 2))
                if end_idx > item_count:
                    end_idx = item_count
                    start_idx = end_idx - WHEIGHT
    
        for i, item in enumerate(itemlist[start_idx:end_idx]):
            color = NORMAL_TEXT
            if start_idx + i == current_item:
                color = HIGHLIGHT_TEXT
            if len(item) > WWIDTH:
                itemstr = item[:WWIDTH-3] + "..."
            else:
                itemstr = item
            # print(" " + selected + " ", end = '')
            MoveCursor(start_x + 1, start_y + 1 + i)
            cprint(itemstr.ljust(WWIDTH), color)
    return ''


def DrawHeader():
    global HEADER_SIZE, HIGHLIGHT_TEXT, SCREEN_X
    centerprint("REVIEW DUPLICATES", 1, HIGHLIGHT_TEXT)
    
    MoveCursor(30, 2)
    print(f'(F)avor this path/file'.ljust(25))

    MoveCursor(30, 3)
    print(f'(R)emove favored paths'.ljust(25))
    
    MoveCursor(30, 4)
    print(f'(D)one with review'.ljust(25))

    MoveCursor(1, 2)
    print(f'* = Already in catalog')
    
    MoveCursor(1, 3)
    print(f'K means keeping')
    
    MoveCursor(1, HEADER_SIZE)
    print(string_n('#',SCREEN_X))
    

# receives a flat index for the filelist, returns a tuple of indexes to duplicates
def DupeIndexTo2D(idx):
    global duplicates
    for i, dupes in enumerate(duplicates):
        ld = len(dupes)
        if ld > idx:
            return (i, idx)
        else:
            idx -= ld
    return (-1, -1)  # should never get here


# counts the total number of duplicates
def CountDupeIndexes():
    global duplicates
    c = 0
    for dupes in duplicates:
        c += len(dupes)
    return c


def DrawMainArea():
    global duplicates, dupe_count, SCREEN_Y, HEADER_SIZE, MAX_FNAME_LENGTH, NUM_FILES_PER_PAGE
    global current_position
    
    # figure out which files we want to display
    if NUM_FILES_PER_PAGE >= dupe_count:  # all on one page. Easy.
        start_idx = 0
        end_idx = dupe_count - 1
    else:
        start_idx = int(current_position - math.floor(NUM_FILES_PER_PAGE / 2))
        if start_idx < 0:
            start_idx = 0
            end_idx = start_idx + (NUM_FILES_PER_PAGE - 1)
        else:
            end_idx = int(current_position + math.ceil(NUM_FILES_PER_PAGE / 2)) - 1
            if end_idx >= dupe_count:
                end_idx = dupe_count - 1
                start_idx = end_idx - (NUM_FILES_PER_PAGE - 1)
    
    # get the 2D indexes
    (si1, si2) = DupeIndexTo2D(start_idx)
    (ei1, ei2) = DupeIndexTo2D(end_idx)


    #MoveCursor(1, 1)
    #print(f'SCREEN_Y:{SCREEN_Y}')
    #print(f'NUM_FILES_PER_PAGE:{NUM_FILES_PER_PAGE}')
    #print(f'start_idx:{start_idx}')
    #print(f'end_idx:{end_idx}')
    #print(f'si1,si2:{si1},{si2}')
    #print(f'ei1,ei2:{ei1},{ei2}')
    
    MoveCursor(1, HEADER_SIZE+1)
    
    row = 0
    duplicates_slice = duplicates[si1:ei1+1]
    for i, dupes in  enumerate(duplicates_slice):
        for i2, d in enumerate(dupes):
            #print(f'i,i2:{i},{i2}')
            if i == 0 and i2 < si2:  # ignore out of view entries at the top
                #print("Under!")
                continue
            if i == len(duplicates_slice) - 1 and i2 >= ei2 + 1:
                #print("Over!")
                continue
            color = NORMAL_TEXT
            if start_idx + row == current_position:
                color = HIGHLIGHT_TEXT
                
            group_highlight = NORMAL_TEXT
            if (i + si1) % 2 == 0:
                group_highlight = HIGHLIGHT_TEXT

            # check if file is in the catalog
            cat = " "
            if d['cat']:
                cat = "*"
            
            keep = " "
            if not d['r']:
                keep = "K"
                
            file = unidecode(d["fp"])  # transliterate unicode characters into ascii
            if len(file) > MAX_FNAME_LENGTH:
                filestr = file[:MAX_FNAME_LENGTH-12] + "..." + file[-9:]
            else:
                filestr = file
            cprint(keep + cat + " ", group_highlight, False)
            cprint(filestr.ljust(MAX_FNAME_LENGTH), color, False)
            cprint("   ", group_highlight)
            row += 1
    #print(f'row:{row}')


def DrawScreen():
    global dupe_count, current_position
    ClearMainArea()
    DrawMainArea()
    
    cp_str = ''
    if dupe_count > 0:
        cp_str = str(current_position + 1) + " out of " + str(dupe_count)
    SetStatusBar('', cp_str)
    

# File analysis stuff  -----------------------------

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


def IsPortableFilename(fname):
    head, tail = os.path.split(fname)
    if re.match("[^a-zA-Z0-9\._\-]", tail) is None:
        return True
    return False
    

# Decide which to keep based on several rules (shortest path, without unicode, favoring certain paths, etc.)
def AnalyzeDupes():
    global to_be_removed, duplicates
    global dr_favored_paths
    for i, dupes in enumerate(duplicates):  # keep the index to the original
        # Go through each of the dupes and assign a score.  Assess when done.
        for i2, d in enumerate(dupes):
            # split out the path and file
            p,f = os.path.split(d['fp'])  # if fp ends in /, f is blank
            score = 0
            # how many folder layers? Up to 14 gives a score of > 0.  Disincentivizes long paths.
            score += (15 - d['fp'].count('/'))
            
            # boost already cataloged files
            if d['cat']:
                score += 5
                
            # incentivize filenames that contain more text characters as a percentage of the total filename
            score += round((len([c for c in f if c.isalpha()]) / len(f)) * 10)
            
            # Does the path have only standard characters?
            if IsPortableFilename(d['fp']):
                score += 4
            
            if d['fp'] in dr_favored_paths:  # user specifically wants this file
                score += 1000
            else:
                # break the path into chunks and iteratively search for them in dr_favored_paths
                # Only one of the iterations should match up with one entry in dr_favored_paths, at most.
                p_chunks = p.lstrip('/').split('/')
                num_chunks = len(p_chunks)
                for c in range(num_chunks):
                    # reassemble parts of the path
                    test_path = "/" + "/".join(p_chunks[0:c+1])
                    if test_path in dr_favored_paths:
                        score += round(((c + 1) / num_chunks) * 100)  # max of 100 points for matching the entire path
            # add the score to the dupes item
            dupes[i2]['s'] = score
        # go through the dupes list to find the highest score
        highest_score = 0
        highest_index = 0  # if scores stay at 0 (highly unlikely), defaults to keeping the first entry
        for i2, d in enumerate(dupes):
            if d['s'] > highest_score:
                highest_score = d['s']
                highest_index = i2
        # then mark that dupe for saving and the others for removal
        for i2 in range(len(dupes)):
            rem = True
            if i2 == highest_index:
                rem = False
            duplicates[i][i2]['r'] = rem


# Add the duplicates flagged for removal to the to_be_removed list.  Only run this once at the end.
def RemoveDupes():
    global to_be_removed, duplicates
    for dupes in duplicates:
        for d in dupes:
            if d['r'] == True:
                to_be_removed.append(d['fp'])


# Execution starts here. ---------------------------
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
        if fullpath in catalog_index:   # we have already cataloged this path... is it the same size?
            entry = catalog[catalog_index[fullpath]]
            if size == entry['s']:  # it's a match (or at least close enough for our purposes), so we don't care about this file.
                filenum += 1
                continue
        if len(file) > 40:
            filestr = file[:31] + "..." + file[-6:]   # for display purposes
        else:
            filestr = file
        if len([i for i in new_data if os.path.join(i['p'], i['f']) == fullpath]) > 0:  # already processed in wip.db
            print(f'DONE - {filestr}',flush=True)
            filenum += 1
            continue
        outline = f'{math.floor(filenum/total_num_files * 100)}% done - ' + \
            f'File #{filenum}  ({round(size/1048576,2)} MB)  {filestr}'.ljust(70)
        print(outline, end="\r", flush=True)
        filenum += 1
        hash = hash_file(fullpath)
        item = { 'h': hash, 'f': file, 'p': root, 's': size }
        if hash in catalog:
            # same exact hash, size, and location is already in the catalog, so don't re-add
            c = catalog[hash]
            if c['f'] == item['f'] and c['p'] == item['p'] and c['s'] == item['s']:
               continue
        new_data.append(item)
        if(time.time() - last_saved > 10):  # only save every 10 seconds
            with open(wip_db_path, 'w') as wipfile:
                json.dump(new_data, wipfile)
            last_saved = time.time()

# save the hash values in wip
with open(wip_db_path, 'w') as wipfile:
    json.dump(new_data, wipfile)

end = time.time()
delta = end - start
print(f'Hash processing time: {round(delta,2)} seconds.')


# identify duplicates
print("\n\rIdentifying duplicates...")
dupes = []  # keeps track of which files we've already discovered are duplicates
for file in new_data:
    hash = file['h']
    fullpath = os.path.join(file['p'], file['f'])
    if fullpath in dupes:   # this is one of the files that we've already discovered and accounted for
        continue
    duplicates_item = []
    # check if its hash is already in the catalog (we've already ignored any cataloged files that have remained unchanged in path)
    catalog_item = {}
    if hash in catalog:  # a newly analyzed file matches one in the catalog
        o = catalog[hash]
        cat_fp = os.path.join(o['p'], o['f'])  # full path to catalog item
        if not os.path.exists(cat_fp):       # If the catalog item was deleted, we don't want to keep this duplicate, either.
            to_be_removed.append(fullpath)   # so add it straight to the to_be_removed list
            continue
        catalog_item['fp'] = cat_fp
        catalog_item['cat'] = True
        if remove_catalog_dupes:   # do we want to be able to remove dupes FROM THE CATALOG?  Default is YES.
            duplicates_item.append(catalog_item)  # add the catalog item as a duplicate to be reviewed

    # now find the duplicates within the new data
    matching = [{'fp': os.path.join(i['p'], i['f']), 'cat': False } for i in new_data if i['h'] == hash and os.path.join(i['p'], i['f']) not in dupes]
    if len(matching) < 2 and not catalog_item:  # there will always be 1 match here
        continue
    duplicates_item.extend(matching)  # add the matching items in new_data to the list of duplicates
    dupes.extend([i['fp'] for i in matching])  # record the fact that we have accounted for those entries
    duplicates.append(duplicates_item)  # add the entry to the main duplicates list


# Only bring the user to the "Review dulicates" page if there are duplicates
if len(duplicates) > 0:
    # Use AnalyzeDupes to go through duplicates and add a 'r': True/False field based on the current rules.
    #    Uses a sort of Bayesian weighted scoring system
    AnalyzeDupes()
    dupe_count = CountDupeIndexes()
    current_position = 0

    # Display the list of duplicates and allow the user to move through them
    # Every time the rules change, re-run AnalyzeDupes
    # Allow the user to save the favored folder settings and load them later?

    os.system("clear")  # clear the screen
    ShowCursor(False)
    DrawHeader()
    DrawScreen()

    # Main loop
    while True:
        key = getKey().lower()
        # Start with handling the keypresses that don't require a list size of > 0
        if key == "esc" or key == 'q':
            r = MakeSelection("Quit?", ["Yes","No"], 20, 2)
            if r == "Yes":
                ShowCursor(True)
                os.system("clear")
                sys.exit()
        elif dupe_count == 0:  # if the list is empty, skip all the other functions
            pass
        elif key == "f":  # Favor path/file
            (i,i2) = DupeIndexTo2D(current_position)
            fp = duplicates[i][i2]['fp']
            favored_path = InputBox("Path to favor:", fp)
            if favored_path:
                dr_favored_paths.append(favored_path)
            AnalyzeDupes()
            pass
        elif key == "r":  # remove preferred paths/files
            rem_path = MakeSelection("Select favored path to remove",['(none)'] + dr_favored_paths, 50, 10)
            if rem_path != '(none)':
                dr_favored_paths.remove(rem_path)
            AnalyzeDupes()
            pass
        elif key == "d":  # done review
            r = MakeSelection("Are you finished with the review?", ["Yes","No"], 35, 2)
            if r == "Yes":
                ShowCursor(True)
                os.system("clear")
                break
            else:
                continue
        elif key == "up":
            if current_position > 0:
                current_position -= 1
        elif key == "down":
            if current_position < dupe_count - 1:
                current_position += 1
        elif key == "pgup":
            if current_position > NUM_FILES_PER_PAGE:
                current_position -= NUM_FILES_PER_PAGE
            else:
                current_position = 0
        elif key == "pgdn":
            if current_position < dupe_count - NUM_FILES_PER_PAGE:
                current_position += NUM_FILES_PER_PAGE
            else:
                current_position = dupe_count - 1
        elif key == "home":
            current_position = 0
        elif key == "end":
            current_position = dupe_count - 1
        else:
            continue  # don't bother to redraw the screen - nothing has changed
        DrawScreen()


    # When finished, run RemoveDupes to add duplicates to the to_be_removed list.  (Only run once.)
    RemoveDupes()

# display the list of files to be removed
if len(to_be_removed) > 0:
    print("Duplicate files to be removed:")
    for f in to_be_removed:
        print(f)
    choice = input(f'Do you want to remove these {len(to_be_removed)} files? (Y/N) ').lower()
    if choice[0] == 'y':
        # do actual file deletion
        for f in to_be_removed:
            if os.path.exists(f):
                os.remove(f)
                print("Removed: ", f)
        print("Finished removing duplicate files.")
    else:
        print("No duplicate files were deleted.")
else:
    print("No duplicates were found.")

# add all the new data to the catalog except for those files that were removed.
new_cat_entries = [i for i in new_data if os.path.join(i['p'], i['f']) not in to_be_removed]

if len(new_cat_entries) > 0:
    choice = input(f'Do you want to add these {len(new_cat_entries)} files to the catalog? (Y/N) ').lower()
    if choice[0] == 'y':
        for i in new_cat_entries:
            if i["h"] in catalog:
                print(f'Hash {i["h"]} for {i["f"]} is already in the catalog!')
            else:
                catalog[i["h"]] = { 'f': i['f'], 'p': i['p'], 's': i['s'] }
        print(f'Added {len(new_cat_entries)} new files to the catalog.')
        with open(catalog_path, 'w') as catalogfile:
            json.dump(catalog, catalogfile)
        os.remove(wip_db_path)
    else:
        print(f'No new files were added to the catalog. WIP has been saved for later.')
else:
    print(f'No new files were added to the catalog.')
    os.remove(wip_db_path)

