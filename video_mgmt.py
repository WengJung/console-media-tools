#!/usr/bin/env python
'''
Tool for managing video files.
This script walks the current folder and all subfolders to get a list of all files.

In order to sort by duration, resolution, etc, we need to first analyze all the video files
and store the results, which is done upon start-up.
The analysis is saved to the database after every 25 files analyzed.  That way, we can deal with
massive amounts of files without having to restart at the beginning if something goes wrong.

The format of the vinfo object is as follows:
{
    'w': width,
    'h': height,
    'fr': framerate,
    'br': bitrate,
    'c': codec,
    'd': duration,
    's': size in kB,
    'ph': pHash value (13 hex digit perceptive hash of a frame of the video.)
}

# How pHash Is Used:
We grab the frame at the 1 second point from each video for analysis.  (Perhaps there is a better
way to do this that avoids common intros, etc.)  If we can save an image fingerprint (pHash), we
can quickly identify which videos appear to be duplicates.  The image fingerprint analysis should
ideally be able to identify similar images regardless of duration, resolution, or color
correction (within reason).

There is a way to get a screensnap if the normal way fails (less than 5% of the time).
This way seems to work, but it's a bit slow and writes to the disk:
https://ottverse.com/thumbnails-screenshots-using-ffmpeg/
ffmpeg -i NoSeek.mp4 -r 1 -s 32x32 -frames:v 1 phash_source.png

When the user checks the pHash, we compare the Hamming distance to all the other hashes, and report
any that are near matches.

The current list of keyboard bindings:
b c d f h m o q r s v z
Ctrl-h


'''

import subprocess
import os
import shutil
import termios
import fcntl
import sys, tty
import math
import time
import json
import readline
import numpy, ffms2  # for DCT for pHash
from PIL import Image
from random import randrange


# misc settings
vinfo_file = "vinfo.db"
target_folders = ["FAVS"]  # defaults

current_position = 0  # which file we are on

CLEAR_SCREEN = "\033[2J"
NORMAL_TEXT = "\033[97;40m"
BOLD_TEXT = "\033[97;100m"
HIGHLIGHT_TEXT = "\033[30;47m"
STATUS_TEXT = HIGHLIGHT_TEXT


def AdjustScreenSize():
    global SCREEN_X, SCREEN_Y, HEADER_SIZE, MAX_FNAME_LENGTH, NUM_FILES_PER_PAGE
    SCREEN_X = os.get_terminal_size().columns
    SCREEN_Y = os.get_terminal_size().lines
    HEADER_SIZE = 5  # number of rows
    MAX_FNAME_LENGTH = SCREEN_X - 6  # max displayed length of filename before shortening
    NUM_FILES_PER_PAGE = SCREEN_Y - (HEADER_SIZE + 1)

AdjustScreenSize()

def ShowCursor(b_show = True):
    if b_show:
        print("\033[?25h", end = "")
    else:
        print("\033[?25l", end = "")


print(NORMAL_TEXT)
os.system("clear")  # clear the screen
ShowCursor(False)
print("Please wait...", end="\r", flush=True)


VIDEO_EXTENSIONS = ['3g2','3gp','3gp2','3gpp','asf','avi','divx','flc','flv','m1v','m4v','mkv','mov','mp4','mpeg','mpg','ts','webm','wmv','vob']

sbres = ''  # sort by res
sbbr = ''   # sort by bitrate
sbfr = ''   # sort by framerate
sbdur = ''   # sort by duration
sbsize = ''   # sort by size
fbcodec = ''  # filter by codec
fbsearch = '' # filter by search term

# check commandline parameters to force a recalculation of all the pHashes
force_phash = False
if 'force_phash' in sys.argv:
    force_phash = True

recheck_phash_errors = False
if 'recheck_phash_errors' in sys.argv:
    recheck_phash_errors = True

# DCT settings for pHash
PH_IMG_SIZE = 32 # image size
NUM_DCT_COEF = 8 # number of DCT coefficients
HAMM_DIST = 5  # max Hamming distance for considering images similar
ERROR_PHASH = "1AAAAAAAAAAAA"  # the phash meaning "error"

# pre-compute stuff for DCT
k = math.sqrt(2.0 / PH_IMG_SIZE)
dct_k = numpy.matrix([
    [k * math.cos((math.pi / 2 / PH_IMG_SIZE) * y * (2 * x + 1)) for x in range(PH_IMG_SIZE)]
    for y in range(1,NUM_DCT_COEF)
])
dct_k_t = numpy.transpose(dct_k)


# When the standard ffms method of getting the frame fails because of not being able to seek,
# this method still works.  It's a little slower and clunkier since it writes a PNG to disk,
# but it should only happen in less than 5% of cases.
# Returns a 32x32 numpy array, or False if it was unable to generate a thumbnail.
def GetNoSeekThumbnail(vid_file):
    global PH_IMG_SIZE
    # calls ffmpeg on the commandline, returns a list of integers representing pixels
    # ffmpeg -i NoSeek.mp4 -r 1 -s 32x32 -frames:v 1 phash_source.png
    thumbnail_file = "phash_source.png"
    if os.path.exists(thumbnail_file):  # get rid of any older thumbnails
        os.remove(thumbnail_file)
    cmd = ["ffmpeg",
           "-i", vid_file,
           "-r", "1",
           "-s", f"{PH_IMG_SIZE}x{PH_IMG_SIZE}",
           "-frames:v", "1",
           thumbnail_file]
    
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if not os.path.exists(thumbnail_file):  # wasn't successful in generating the thumbnail
        return False
    
    # load the PNG pixels as an array of ints
    img = Image.open(thumbnail_file).convert('L')  # load and convert to greyscale
    os.remove(thumbnail_file)  # clean up
    return numpy.array(img.getdata())
    

# Since I don't know how to do time-based frame-accurate seeking, we just use
# frame number based seeking for now.
def GetPHashForFrame(vid_file, frameno):
    global PH_IMG_SIZE, dct_k, dct_k_t, ERROR_PHASH
    try:
        vsource = ffms2.VideoSource(vid_file)  # automatically indexes it on loading.  This too slow?
        vsource.set_output_format([ffms2.get_pix_fmt("gray")], width=PH_IMG_SIZE, height=PH_IMG_SIZE)
        # vsource.get_frame(frameno)  gets the specified frame from the video.  It comes in as a single long array.
        img_data = vsource.get_frame(frameno).planes[0]
    except:  # if we ran into trouble getting the video frame the standard way, use the clunkier method
        img_data = GetNoSeekThumbnail(vid_file)  # we ignore the frameno, sadly
        if img_data is False:
            return ERROR_PHASH

    # .reshape((N,N)).astype(float)  reshapes the array into a multidimensional array of NxN floats
    data = img_data.reshape((PH_IMG_SIZE,PH_IMG_SIZE)).astype(float)
    # Do the Discrete Cosine Transform matrix math to return with 49 coefficients
    coefs = numpy.array(dct_k * data * dct_k_t).flatten()
    median = numpy.median(coefs)
    h = sum((1<<i) for i,j in enumerate(coefs) if j > median)  # create the pHash bit by bit
    return ("%013x" % h)  # 49 bits can only give us up to 0x1FFFFFFFFFFFFF
    

def IsUnderHammingDistance(n1, n2, maxamt):
    diff = n1 ^ n2
    count = 0
    while diff > 0:  # check all the bits
        if diff & 1:
            count += 1
            if count > maxamt:  # too big of a difference
                return False
        diff = diff >> 1
    return True


def FindSimilarPHashes(phash):
    global vinfo
    new_list = []
    ph = int("0x"+phash, base=16)
    files = list(vinfo.keys())
    phashes = [int("0x"+vinfo[k]['ph'], base=16) for k in files]
    for idx, f in enumerate(files):
        if(IsUnderHammingDistance(ph,phashes[idx], HAMM_DIST)):
            new_list.append(f)
    return new_list


def FindNextSetOfVisualDuplicates():
    global vinfo, workinglist, current_position
    # pre-load all the int pHashes...
    files = list(vinfo.keys())
    phashes = [int("0x"+vinfo[k]['ph'], base=16) for k in files]
    starting_position = current_position
    for i,f in enumerate(workinglist[starting_position:]):
        fph = int("0x"+vinfo[f]['ph'], base=16)
        # how many others are visually similar?
        new_list = []
        for i2, lph in enumerate(phashes):
            if(IsUnderHammingDistance(fph, lph, HAMM_DIST)):
                new_list.append(files[i2])
        # update the UI
        current_position = i + starting_position
        DrawScreen()
        if len(new_list) > 1:  # we always match with ourselves
            return new_list
    return []   # empty list means there are no more duplicates from current_position through to the end.
    

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
            #print(sequence)
            return ''  # didn't recognized it
    elif firstChar == '\x08':  # Ctrl-H
        return 'Ctrl-H'
    else:
        return firstChar


def rlinput(prompt, prefill=''):
    readline.set_startup_hook(lambda: readline.insert_text(prefill))
    try:
        return input(prompt)
    finally:
        readline.set_startup_hook()


def GetFileSize(file):
    return round(os.path.getsize(file) / 1024, 1)


def GetVideoInfo(file):
    # run ffprobe and collect the results
    cmd = ['ffprobe', '-i', file, '-v', 'quiet', '-select_streams', 'v:0', '-show_entries', 'stream=bit_rate,avg_frame_rate,height,width,codec_tag_string,duration', '-hide_banner']
    probe_lines = subprocess.run(cmd, capture_output=True, text=True).stdout.split('\n')
    w = 0
    h = 0
    fr = 0
    br = 0
    c = ""
    d = 0.0
    for line in probe_lines:
        if line.startswith('width'):
            w = int(line.split('=')[-1])
        elif line.startswith('height'):
            h = int(line.split('=')[-1])
        elif line.startswith('bit_rate'):
            val = line.split('=')[-1]
            if 'N/A' not in val:   # no bitrate specified?  calculate a rough approximate bitrate from other things later
                br = int(val)
        elif line.startswith('avg_frame_rate'):
            val = line.split('=')[-1]
            if '/' in val:
                top, bot = val.split('/')
                if float(bot) != 0:
                    fr = round(float(float(top) / float(bot)))
            else:
                fr = round(float(val))
        elif line.startswith('codec_tag_string'):
            c = line.split('=')[-1].lower()
        elif line.startswith('duration'):
            val = line.split('=')[-1]
            if 'N/A' not in val:
                d = round(float(val),1)
    pHash = GetPHashForFrame(file, fr * 1)  # save the pHash for the frame 1 second in
    return { 'w': w, 'h': h, 'fr': fr, 'br': br, 'c': c, 'd': d , 's': GetFileSize(file), 'ph': pHash }


def ReadVinfoDB():
    global vinfo, vinfo_file
    vinfo = {}
    if os.path.exists(vinfo_file):
        with open(vinfo_file, 'r') as vinfofile:
            vinfo = json.load(vinfofile)


def WriteVinfoDB():
    global vinfo, vinfo_file
    with open(vinfo_file, 'w') as vinfofile:
        json.dump(vinfo, vinfofile)


def AnalyzeFiles():
    global VIDEO_EXTENSIONS, filelist, file_count, fileselection, vinfo
    global force_phash, recheck_phash_errors, ERROR_PHASH
    global SCREEN_X
    
    MAX_FNAME_LEN = SCREEN_X - 35
    
    starting_path = u"."
    # walk the folder tree, creating a list first.
    filelist = [os.path.relpath(os.path.join(dp, f), starting_path) for (dp, _, fnames) in os.walk(starting_path) for f in fnames \
     if f.split('.')[-1].lower() in VIDEO_EXTENSIONS]  # ignore our db files, etc.
    file_count = len(filelist)
    fileselection = []
    
    # try to load up any local database file.
    ReadVinfoDB()
    # remove any entries for files that are not present.
    for k in list(vinfo.keys()):
        if k not in filelist:
            del vinfo[k]
        # file exists, but we're redoing all pHashes, or rechecking errors
        elif force_phash or (vinfo[k]['ph'] == ERROR_PHASH and recheck_phash_errors):
            vinfo[k]['ph'] = GetPHashForFrame(k, vinfo[k]['fr'] * 1)  # save the pHash for the frame 1 second in
            
    # Then, analyze and add any files that are not in the db
    to_analyze = [f for f in filelist if f not in vinfo]
    filenum = 0
    to_analyze_count = len(to_analyze)
    for file in to_analyze:
        filenum += 1
        if os.path.islink(file):   # ignore link files
            continue
        ext = file.split('.')[-1].lower()
        if ext in VIDEO_EXTENSIONS:
            # print which file we're working on
            if len(file) > MAX_FNAME_LEN:
                filestr = "..." + file[-MAX_FNAME_LEN:]
            else:
                filestr = file
            outline = f'{math.floor(filenum/to_analyze_count * 100)}% done - ' + \
                      f'File #{filenum} of {to_analyze_count}  {filestr}'
            print(outline.ljust(SCREEN_X-1), end="\r", flush=True)
            # analyze the file
            vi = GetVideoInfo(file)
            vinfo[file] = vi
        if filenum % 25 == 0:  # write the vinfo file after every 25 files
            WriteVinfoDB()
    WriteVinfoDB()      #  and also at the end


def cprint(str, color="\033[97;44m", newline = True):
    if not newline:
        print(color + str + NORMAL_TEXT, flush = True, end='')
    else:
        print(color + str + NORMAL_TEXT, flush = True)


def MoveCursor(x, y):
    print(f"\033[{y};{x}H", end = "")


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


def DrawWindow(title, w, h):  # auto-center this
    global SCREEN_X, SCREEN_Y
    start_x = math.floor((SCREEN_X - w) / 2)
    start_y = math.floor((SCREEN_Y - h) / 2)
    doshadow = False
    shadow = ''
    offs_x = 0
    if start_x > 2 and start_y > 2:
        offs_x = 1
        shadow = ' '
        doshadow = True
    if doshadow:
        MoveCursor(start_x - offs_x, start_y - 1)
        print(f'{" "*(w+2)}')
    if len(title) > w-2:
        title = title[:-(w-5)] + "..."
    MoveCursor(start_x - offs_x, start_y)
    print(f'{shadow}╔{title.ljust(w-2,"═")}╗{shadow}')
    for y in range(start_y + 1, start_y + h - 1):
        MoveCursor(start_x - offs_x, y)
        print(f'{shadow}║{" "*(w-2)}║{shadow}')
    MoveCursor(start_x - offs_x, start_y + h - 1)
    print(f'{shadow}╚{"═"*(w-2)}╝{shadow}')
    if doshadow:
        MoveCursor(start_x - offs_x, start_y + h)
        print(f'{" "*(w+2)}')
    return (start_x, start_y)


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
    
    
def InfoBox(title, textrows, WWIDTH, WHEIGHT):
    start_x, start_y = DrawWindow(title, WWIDTH + 2, WHEIGHT + 2)
    current_pos_x = 0
    current_pos_y = 0
    row_count = len(textrows)
    longest_row_len = len(max(textrows, key = len))
    movement_y = row_count - WHEIGHT
    if movement_y < 0:
        movement_y = 0
    movement_x = longest_row_len - WWIDTH
    if movement_x < 0:
        movement_x = 0

    first = True
    while True:
        if first:   # kludge to make sure we draw the info first.
            first = False
        else:
            key = getKey().lower()
            if key == "up":
                if current_pos_y > 0:
                    current_pos_y -= 1
            elif key == "down":
                if current_pos_y < movement_y:
                    current_pos_y += 1
            elif key == "left":
                if current_pos_x > 0:
                    current_pos_x -= 1
            elif key == "right":
                if current_pos_x < movement_x:
                    current_pos_x += 1
            elif key == '\r':
                return ''
            elif key == 'esc':
                return ''
            else:
                continue
        # drawing section
        for i, row in enumerate(textrows[current_pos_y:current_pos_y+WHEIGHT]):
            if len(row) == 0:
                MoveCursor(start_x + 1, start_y + 1 + i)
                cprint(" ".ljust(WWIDTH), NORMAL_TEXT)  # blank out the row
                continue
            color = NORMAL_TEXT
            if row[0] == "#":
                color = BOLD_TEXT
                row = row[1:]  # Trim it off so we don't see it
            if current_pos_x > 0:
                rowstr = "«" + row[current_pos_x + 1:]
            else:
                rowstr = row
            if len(rowstr) > WWIDTH:
                rowstr = rowstr[:WWIDTH-1] + "»"
            MoveCursor(start_x + 1, start_y + 1 + i)
            
            cprint(rowstr.ljust(WWIDTH), color)
    return ''


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


def DrawHeader():
    global HEADER_SIZE, SCREEN_X, sbres, sbbr, sbfr, sbdur, sbsize, fbcodec, fbsearch, in_sublist
    AdjustScreenSize()
    centerprint("VIDEO MANAGEMENT", 1, HIGHLIGHT_TEXT)
    
    outer_margins = 1
    inner_margins = 2
    numcols = 3
    colwidth = math.floor((SCREEN_X - (outer_margins * 2) - (inner_margins * (numcols - 1))) / numcols)
    col1x = outer_margins + 1
    col2x = col1x + colwidth + inner_margins
    col3x = col2x + colwidth + inner_margins

    if in_sublist:  # we're in a sub-list, so don't show the normal options
        MoveCursor(col1x, 2)
        print(f'Press left arrow to return'.ljust(colwidth))
    else:
        MoveCursor(col1x, 2)
        print(f'Sort (h)eight: {sbres}'.ljust(colwidth))

        MoveCursor(col1x, 3)
        print(f'Sort (f)ramerate: {sbfr}'.ljust(colwidth))

        MoveCursor(col1x, 4)
        print(f'Sort (b)itrate: {sbbr}'.ljust(colwidth))

        MoveCursor(col2x, 2)
        print(f'Sort (d)uration: {sbdur}'.ljust(colwidth))
        
        MoveCursor(col2x, 3)
        print(f'Sort si(z)e: {sbsize}'.ljust(colwidth))
        
        MoveCursor(col2x, 4)
        print(f'Filter c(o)dec: {fbcodec}'.ljust(colwidth))
        
        MoveCursor(col3x, 2)
        print(f'(S)earch term: {fbsearch}'.ljust(colwidth))
        
        
        MoveCursor(col3x, 4)
        print(f'(Ctrl-H) Help'.ljust(colwidth))
        


    MoveCursor(1, HEADER_SIZE)
    print(string_n('#',SCREEN_X))


def DisplayHelp():
    textrows = [
        "# Navigation",
        "Up/Down - Move cursor one file at a time",
        "PageUp/PageDn - Move cursor one page at a time",
        "Home - Move cursor to first file in list",
        "End - Move cursor to last file in list",
        "Left - Return from sublist, if applicable",
        "    (Only applies to the V command below.)",
        "    Sorting, filtering, and searching commands do not",
        "    work while in a sublist.",
        "? - Move cursor to random file in list",
        "Esc or Q - Quit the program",
        "",
        "# Sorting",
        "H - Sort by video height",
        "B - Sort by video bitrate",
        "F - Sort by video frame rate",
        "D - Sort by video duration",
        "Z - Sort by file size",
        "",
        "# Filtering and Searching",
        "S - Search for matching filenames (case insensitive)",
        "O - Filter by video codec",
        "V - Find visually similar files based on image at 1 sec.",
        "    This searches the entire list of files, even if a",
        "    filter or search term is in effect. Results are",
        "    displayed as a sublist.  Similarity is determined",
        "    using a perceptive hashing function that should",
        "    identify similar files regardless of quality and",
        "    resolution.",
        "",
        "# File commands",
        "<enter> - Play video file using mpv",
        "<space> - Select file(s) (for working with multiple files)",
        "<del> - Delete the file(s)",
        "    This command does not prompt you for confirmation,",
        "    so be careful!",
        "R - Rename file",
        "    This will not allow you to overwrite an existing",
        "    file of the target name.",
        "C - Copy to named folder",
        "    Allows user to select a folder (or specify an addition",
        "    to the list) to copy the file(s) into.",
        "M - Move to named folder",
        "    Allows user to select a folder (or specify an addition",
        "    to the list) to move the file into.",
        "T - Trim the video file",
        "    Allows user to trim a video file based on start/stop",
        "    times. Creates a new file.",
        "",
        "# Misc",
        "Ctrl-H - View this help dialog",
        "",
        "# General info",
        "Upon startup, the program begins to analyze all video files",
        "in the current folder and subfolders.  It creates a database",
        "file called vinfo.db, which helps with being able to sort,",
        "filter, and search through the files.  This can take a while",
        "on the first startup (depending on various factors), but then",
        "only needs to be updated for files that have changed.",
        "The analysis is done by the ffprobe utility.",
        "",
    ]
    
    InfoBox("Help", textrows, 62, 20)


def ClearMainArea():
    global HEADER_SIZE
    MoveCursor(1, HEADER_SIZE+1)
    print("\033[J", end = "")  # clears everything below!


def DrawMainArea():
    global workinglist, working_count, SCREEN_Y, HEADER_SIZE, MAX_FNAME_LENGTH, NUM_FILES_PER_PAGE, fileselection
    global current_position

    MoveCursor(1, HEADER_SIZE+1)
    # figure out which files we want to display
    if NUM_FILES_PER_PAGE >= working_count:  # all on one page. Easy.
        start_idx = 0
        end_idx = working_count
    else:
        start_idx = int(current_position - math.floor(NUM_FILES_PER_PAGE / 2))
        if start_idx < 0:
            start_idx = 0
            end_idx = start_idx + NUM_FILES_PER_PAGE
        else:
            end_idx = int(current_position + math.ceil(NUM_FILES_PER_PAGE / 2))
            if end_idx > working_count:
                end_idx = working_count
                start_idx = end_idx - NUM_FILES_PER_PAGE

    for i, file in enumerate(workinglist[start_idx:end_idx]):
        color = NORMAL_TEXT
        if start_idx + i == current_position:
            color = HIGHLIGHT_TEXT

        # check if file is selected
        if file in fileselection:
            selected = "#"
        else:
            selected = " "  # or #
        if len(file) > MAX_FNAME_LENGTH:
            filestr = file[:MAX_FNAME_LENGTH-12] + "..." + file[-9:]
        else:
            filestr = file
        print(" " + selected + " ", end = '')
        cprint(filestr, color)


def SetStatusBar(left="", right=""):
    global SCREEN_X, SCREEN_Y
    string = left + string_n(" ",SCREEN_X - (len(left) + len(right))) + right
    MoveCursor(1,SCREEN_Y)
    cprint(string, STATUS_TEXT, False)
    MoveCursor(1, 1)


def DrawScreen():
    global working_count, current_position, workinglist
    DrawScreen.count += 1
    if DrawScreen.count % 10:
        MoveCursor(1, 1)
        print("\033[J", end = "")  # clears everything below!
        DrawHeader()
    ClearMainArea()  # clears to the end of the screen, so nukes the status bar, too.
    DrawMainArea()

    cp_str = ''
    vi = ''
    if working_count > 0:
        cp_str = str(current_position + 1) + " out of " + str(working_count)
        vi = VideoInfoString(workinglist[current_position])
    SetStatusBar(cp_str, vi)
DrawScreen.count = 0  # initialize count


def VideoInfoString(key):
    global vinfo
    i = vinfo[key]
    vi = f'size:{i["s"]}kB, br:{round(i["br"]/1000)}k, res:{i["w"]}x{i["h"]}, {i["fr"]}fps, dur:{i["d"]}s'
    return vi


def PlayVideo(file):
    # launch mpv
    cmd = ['mpv', file, '--keep-open']
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def TrimVideo(file):
    # ffmpeg -i input.mp4 -ss 00:05:10 -to 00:15:30 -c:v copy -c:a copy output2.mp4
    start_time = InputBox("Start time as HH:MM:SS ", "")
    if not start_time:
        return False
    stop_time = InputBox("Stop time as HH:MM:SS ", "")
    if not stop_time:
        return False
    
    f = file.rsplit('.', 1)
    outputfile = 'NONE'
    while outputfile == 'NONE':
        outputfile = InputBox("Name of output file:", f[0] + ' (trimmed).' + f[1])
        if outputfile == '':
            return False
        if os.path.exists(outputfile):
            MakeSelection("That file name already exists!", ['Ok'], 36, 5)
            outputfile = 'NONE'
    
    cmd = ['ffmpeg', '-i', file, '-ss', start_time, '-to', stop_time, '-c:v', 'copy', '-c:a', 'copy', outputfile]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return outputfile


def FilterAndSortFiles():
    global workinglist, working_count, fileselection, current_position, vinfo
    global sbres, sbbr, sbfr, sbdur, sbsize, fbcodec, fbsearch
    if fbcodec != '':
        workinglist = [i for i in filelist if vinfo[i]['c'] == fbcodec]
    else:
        workinglist = [i for i in filelist]
        
    # search filenames case-insensitive
    if fbsearch != '':
        workinglist = [i for i in workinglist if fbsearch.lower() in i.lower()]

    rv = 0  # resolution vector
    if sbres == 'asc':
        rv = 1
    elif sbres == 'desc':
        rv = -1

    frv = 0  # framerate vector
    if sbfr == 'asc':
        frv = 1
    elif sbfr == 'desc':
        frv = -1

    brv = 0  # bitrate vector
    if sbbr == 'asc':
        brv = 1
    elif sbbr == 'desc':
        brv = -1

    drv = 0  # duration vector
    if sbdur == 'asc':
        drv = 1
    elif sbdur == 'desc':
        drv = -1
        
    sv = 0  # size vector
    if sbsize == 'asc':
        sv = 1
    elif sbsize == 'desc':
        sv = -1
        
    workinglist = sorted(workinglist, key=lambda x: (rv * vinfo[x]['h'], \
                                                    frv * vinfo[x]['fr'], \
                                                    brv * vinfo[x]['br'], \
                                                    drv * vinfo[x]['d'], \
                                                    sv * vinfo[x]['s'], \
                                                    x))
    working_count = len(workinglist)
    fileselection.clear()
    current_position = 0


in_sublist = False
def PushWorkingList():
    global workinglist, fileselection, current_position
    global s_workinglist, s_fileselection, s_current_position, in_sublist
    if in_sublist:  # only try to push if there's nothing stored.
        return
    s_current_position = current_position
    s_fileselection = fileselection
    s_workinglist = workinglist
    in_sublist = True
    

def PopWorkingList():
    global workinglist, working_count, fileselection, current_position
    global s_workinglist, s_fileselection, s_current_position, in_sublist
    if not in_sublist:  # only try to pop if something has been pushed.
        return
    current_position = s_current_position
    fileselection = s_fileselection
    workinglist = s_workinglist
    working_count = len(workinglist)
    in_sublist = False


# start displaying things
print("Analyzing files...")
AnalyzeFiles()
used_codecs = sorted(set([o['c'] for o in vinfo.values()]))
FilterAndSortFiles()   # sets up the basic workinglist
os.system("clear")  # clear the screen
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
    elif key == 'h' and not in_sublist:  # sort by height
        if sbres == '':
            sbres = 'desc'
        elif sbres == 'desc':
            sbres = 'asc'
        elif sbres == 'asc':
            sbres = ''
        FilterAndSortFiles()
        DrawHeader()
    elif key == 'b' and not in_sublist:  # sort by bitrate
        if sbbr == '':
            sbbr = 'desc'
        elif sbbr == 'desc':
            sbbr = 'asc'
        elif sbbr == 'asc':
            sbbr = ''
        FilterAndSortFiles()
        DrawHeader()
    elif key == 'f' and not in_sublist:  # sort by frame rate
        if sbfr == '':
            sbfr = 'desc'
        elif sbfr == 'desc':
            sbfr = 'asc'
        elif sbfr == 'asc':
            sbfr = ''
        FilterAndSortFiles()
        DrawHeader()
    elif key == 'd' and not in_sublist:  # sort by duration
        if sbdur == '':
            sbdur = 'desc'
        elif sbdur == 'desc':
            sbdur = 'asc'
        elif sbdur == 'asc':
            sbdur = ''
        FilterAndSortFiles()
        DrawHeader()
    elif key == 'z' and not in_sublist:  # sort by size
        if sbsize == '':
            sbsize = 'desc'
        elif sbsize == 'desc':
            sbsize = 'asc'
        elif sbsize == 'asc':
            sbsize = ''
        FilterAndSortFiles()
        DrawHeader()
    elif key == 's' and not in_sublist:  # filter by search term
        fbsearch = InputBox("Enter search term:", fbsearch)
        FilterAndSortFiles()
        DrawHeader()
    elif key == 'o' and not in_sublist:  # filter by codec
        fbcodec = MakeSelection("Select codec",['(none)'] + used_codecs, 25, 10)
        if fbcodec == '(none)':
            fbcodec = ''
        FilterAndSortFiles()
        DrawHeader()
    elif key == "left":    # return from a sublist
        PopWorkingList()
        DrawHeader()
    elif len(workinglist) == 0:  # if the working list is empty, skip all the other functions
        pass
    elif key == " ":
        file = workinglist[current_position]
        if file in fileselection:
            fileselection.remove(file)
        elif file not in fileselection:
            fileselection.append(file)
    elif key == "up":
        if current_position > 0:
            current_position -= 1
    elif key == "down":
        if current_position < working_count - 1:
            current_position += 1
    elif key == "pgup":
        if current_position > NUM_FILES_PER_PAGE:
            current_position -= NUM_FILES_PER_PAGE
        else:
            current_position = 0
    elif key == "pgdn":
        if current_position < working_count - NUM_FILES_PER_PAGE:
            current_position += NUM_FILES_PER_PAGE
        else:
            current_position = working_count - 1
    elif key == "home":
        current_position = 0
    elif key == "end":
        current_position = working_count - 1
    elif key == "\r":  # enter to play video
        file = workinglist[current_position]
        SetStatusBar("Playing video " + str(current_position + 1) + " out of " + str(working_count) + "...",VideoInfoString(file))
        PlayVideo(file)  # waits for video player to return
    elif key == "del":  # delete the file(s)
        # check if files are selected
        if len(fileselection) > 0:
            for f in fileselection:
                os.remove(f)
                filelist.remove(f)
                workinglist.remove(f)
                del vinfo[f]
                if in_sublist:  # we're doing operations to files in the sublist; update the main list, too.
                    try:
                        s_fileselection.remove(f)
                    except:
                        pass  # ignore any errors
                    try:
                        s_workinglist.remove(f)
                    except:
                        pass  # ignore any errors
                    s_wc = len(s_workinglist)
                    if s_current_position >= s_wc:  # check that stored current position is still valid
                        s_current_position = s_wc - 1
            fileselection.clear()
        else:  # just delete the current one
            file = workinglist[current_position]
            os.remove(file)
            filelist.remove(file)
            workinglist.remove(file)
            del vinfo[file]
            if in_sublist:  # we're doing operations to files in the sublist; update the main list, too.
                try:
                    s_fileselection.remove(file)
                except:
                    pass  # ignore any errors
                try:
                    s_workinglist.remove(file)
                except:
                    pass  # ignore any errors
                s_wc = len(s_workinglist)
                if s_current_position >= s_wc:  # check that stored current position is still valid
                    s_current_position = s_wc - 1
            
        file_count = len(filelist)
        working_count = len(workinglist)
        WriteVinfoDB()
        if current_position >= working_count:  # check that current position is still valid
            current_position = working_count - 1
    elif key == "c":  # copy the file(s)
        # show the list of folders available
        target_folder = MakeSelection("Select folder", target_folders + ['(new)'], 25, 10)
        if target_folder == '':
            DrawScreen()
            continue
        if target_folder == '(new)':
            target_folder = InputBox("Enter name of new folder:","")
            if target_folder == '':
                DrawScreen()
                continue
            target_folders.append(target_folder)   # save for later
        os.makedirs(target_folder, exist_ok=True)  # create the folder if it doesn't exist
        
        # check if files are selected
        if len(fileselection) > 0:
            for f in fileselection:
                base = os.path.basename(f)
                newfile = os.path.join(target_folder, base)
                if newfile == f:  # file already exists in target location
                    continue
                if os.path.exists(newfile):
                    MakeSelection("That destination file name already exists!", ['Ok'], 36, 5)
                    DrawScreen()
                    continue
                # copy the file
                shutil.copy(f, newfile)

                # copy the info/lists for the copied files
                filelist.append(newfile)
                workinglist.append(newfile)
                vinfo[newfile] = vinfo[f]
                if in_sublist:  # we're doing operations to files in the sublist; update the main list, too.
                    try:
                        s_workinglist.append(newfile)
                    except:
                        pass  # ignore any errors
                    s_wc = len(s_workinglist)
            fileselection.clear()
        else:  # just copy the current one
            file = workinglist[current_position]
            base = os.path.basename(file)
            newfile = os.path.join(target_folder, base)
            if newfile == file:  # file already exists in target location
                continue
            if os.path.exists(newfile):
                MakeSelection("That destination file name already exists!", ['Ok'], 36, 5)
                DrawScreen()
                continue
            
            # copy the file
            shutil.copy(file, newfile)
            
            filelist.append(newfile)
            workinglist.append(newfile)
            vinfo[newfile] = vinfo[file]
            if in_sublist:  # we're doing operations to files in the sublist; update the main list, too.
                try:
                    s_workinglist.append(newfile)
                except:
                    pass  # ignore any errors
                s_wc = len(s_workinglist)

        file_count = len(filelist)
        working_count = len(workinglist)
        WriteVinfoDB()
    elif key == "r":  # rename the file
        file = workinglist[current_position]
        dir = os.path.dirname(file)
        base = os.path.basename(file)
        newbase = InputBox("Rename file:", base)
        newfile = os.path.join(dir, newbase)
        if os.path.exists(newfile):
            MakeSelection("That file name already exists!", ['Ok'], 36, 5)
            DrawScreen()
            continue
        os.rename(file, newfile)
        flidx = filelist.index(file)  # find the index of that file 
        filelist[flidx] = newfile
        workinglist[current_position] = newfile
        # update the vinfo db
        temp = vinfo[file]
        del vinfo[file]
        vinfo[newfile] = temp
        if in_sublist:  # we're doing operations to files in the sublist; update the main list, too.
            try:
                s_fileselection[s_fileselection.index(file)] = newfile
            except:
                pass  # ignore any errors
            try:
                s_workinglist[s_workinglist.index(file)] = newfile
            except:
                pass  # ignore any errors
        WriteVinfoDB()
    elif key == "m":  # move to named folder
        # show the list of folders available
        target_folder = MakeSelection("Select folder", target_folders + ['(new)'], 25, 10)
        if target_folder == '':
            DrawScreen()
            continue
        if target_folder == '(new)':
            target_folder = InputBox("Enter name of new folder:","")
            if target_folder == '':
                DrawScreen()
                continue
            target_folders.append(target_folder)   # save for later

        file = workinglist[current_position]
        base = os.path.basename(file)
        os.makedirs(target_folder, exist_ok=True)  # create the folder if it doesn't exist
        newfile = os.path.join(target_folder, base)
        if newfile == file:  # file already exists in target location
            continue
        if os.path.exists(newfile):
            MakeSelection("That file name already exists!", ['Ok'], 36, 5)
            DrawScreen()
            continue
        os.rename(file, newfile)
        flidx = filelist.index(file)  # find the index of that file 
        filelist[flidx] = newfile
        workinglist[current_position] = newfile
        # update the vinfo db
        temp = vinfo[file]
        del vinfo[file]
        vinfo[newfile] = temp
        if in_sublist:  # we're doing operations to files in the sublist; update the main list, too.
            try:
                s_fileselection[s_fileselection.index(file)] = newfile
            except:
                pass  # ignore any errors
            try:
                s_workinglist[s_workinglist.index(file)] = newfile
            except:
                pass  # ignore any errors
        WriteVinfoDB()
    elif key == "v" and not in_sublist:  # find next set of visually similar files
        dupe_list = FindNextSetOfVisualDuplicates()
        if len(dupe_list) == 0:
            MakeSelection("No duplicates found beyond this file!", ['Ok'], 42, 1)
            DrawScreen()
            continue
        PushWorkingList()
        workinglist = dupe_list
        working_count = len(workinglist)
        fileselection.clear()
        current_position = 0
        DrawHeader()
    elif key == "?":  # select random item from list
        current_position = randrange(0, working_count)
    elif key == "ctrl-h":
        DisplayHelp()
    elif key == "t":  # trim video
        outputfile = TrimVideo(workinglist[current_position])
        if outputfile == False:
            continue
        MakeSelection("File was trimmed.", ['Ok'], 36, 5)
        
        filelist.append(outputfile)
        workinglist.append(outputfile)
        vinfo[outputfile] = GetVideoInfo(outputfile)  # calc new vinfo for that file.
        
        if in_sublist:  # we're doing operations to files in the sublist; update the main list, too.
            try:
                s_workinglist.append(outputfile)
            except:
                pass  # ignore any errors
            s_wc = len(s_workinglist)

        file_count = len(filelist)
        working_count = len(workinglist)
        WriteVinfoDB()
    else:
        continue  # don't bother to redraw the screen - nothing has changed
    DrawScreen()

