# console-media-tools
Console-based text UI tools for managing videos and images

# Requirements
This project requires ffmpeg to be installed, as well as a wrapper library:
    apt install ffmpeg libffms2-4
    
To play videos, 'mpv' is used, which can be installed like this:
    apt install mpv

It also requires Python 3.7+ and the following Python packages to be installed:
    pip install numpy xxhash unidecode ffms2

# Subprojects
## video_mgmt.py
Tool for managing video files. This script walks the current folder and all subfolders to analyze all video files. It stores video information such as duration, resolution, bitrate, size, and perceptive hash (pHash) in a JSON database file called vinfo.db.  Then, it provides a text user interface to browse, search, filter, play, move, rename, and find visually similar videos.

## catalog_files.py
This script recursively analyzes a set of folders containing files,
* identifies which files are duplicates based on the xxhash function
* provides a user interface to help pick which files to keep
* removes the duplicates
* creates (or adds to) a centralized JSON catalog of all the file names, paths, sizes, and file hashes.

Existing catalog items will be used to determine whether the analyzed files should be added to the catalog or discarded. Do not run the script on already cataloged files unless they are completely unchanged (i.e. not renamed).  It will attempt to delete them if the path and filename do not perfectly match the catalog.  (See also the update_catalog_locations.py script.)
The deletions only come at the end of the script, after interacting with the user.

Note: The catalog location can be set with an environment variable named 'CATALOG_DB_PATH'. If that variable is not present, the path defaults to the user's home folder.

## update_catalog_locations.py
Analyzes files in the current folder and subfolder to see if they exist in the catalog, but have been renamed or moved. The script updates the catalog to match the new name/location.

## find_noncatalog_files.py
The purpose of this script is to recursively analyze a set of folders containing files, checking against a JSON catalog for the current file names, paths, and sizes.  Any files in the folder or subfolders that are not found in the list (or whose name or size changed) will be hashed to check for a renamed match.  It will then output a list of "changed + matched" and "unmatched" files for the user to examine further.

## rmemptydirs.py
Simple script walks the tree of the current folder and removes any empty folders.


