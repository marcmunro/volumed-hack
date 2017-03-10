<?php

/**
 * This Program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 3, or (at your option)
 * any later version.
 *
 * This Program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with Moode; see the file COPYING. If not, see
 * <http://www.gnu.org/licenses/>.
 *
 * Moode Audio Player (c) 2015 Andreas Goetz <cpuidle@gmx.de>
 * http://moodeaudio.org
 *
 * 2015-10-30: AG initial version
 * - script looks for flac, mp3 or m4a embedded art, Folder, folder, Cover, cover png/jpg/jpeg files, or any other image file.
 * - call via /coverart.php/some/local/file/name
 * - make sure client is configured to hand cover requests to /coverart.php or setup an nginx catch-all rule:
 * - try_files $uri $uri/ /coverart.php;
 *
 * 2016-06-07: TC moodeOS 1.0
 *
 */

set_include_path('inc');
require_once dirname(__FILE__) . '/inc/playerlib.php'; // for debugLog() if needed

function outImage($mime, $data) {
	switch ($mime) {
		case "image/png":
		case "image/gif":
		case "image/jpg":
		case "image/jpeg":
			header("Content-Type: " . $mime);
			echo $data;
			exit(0);
			break;
		default :
			break;
	}
}

function getImage($path) {
	global $getid3;

	if (!file_exists($path)) {
		return false;
	}

	$ext = pathinfo($path, PATHINFO_EXTENSION);

	switch (strtolower($ext)) {
		case 'png':
		case 'jpg':
		case 'jpeg':
			// physical image file -> redirect
			$path = '/mpdmusic' . substr($path, strlen('/var/lib/mpd/music'));
			$path = str_replace('#', '%23', $path);
			header('Location: ' . $path);
			die;

			// alternative -> return image file contents
			$mime = 'image/' . $ext;
			$data = file_get_contents($path);

			outImage($mime, $data);
			break;

		case 'mp3':
			require_once 'Zend/Media/Id3v2.php';
			try {
				$id3 = new Zend_Media_Id3v2($path);

				if (isset($id3->apic)) {
					outImage($id3->apic->mimeType, $id3->apic->imageData);
				}
			}
			catch (Zend_Media_Id3_Exception $e) {
				// catch any parse errors
			}

			require_once 'Zend/Media/Id3v1.php';
			try {
				$id3 = new Zend_Media_Id3v1($path);

				if (isset($id3->apic)) {
					outImage($id3->apic->mimeType, $id3->apic->imageData);
				}
			}
			catch (Zend_Media_Id3_Exception $e) {
				// catch any parse errors
			}
			break;

		case 'flac':
			require_once 'Zend/Media/Flac.php';
			try {
				$flac = new Zend_Media_Flac($path);

				if ($flac->hasMetadataBlock(Zend_Media_Flac::PICTURE)) {
					$picture = $flac->getPicture();
					outImage($picture->getMimeType(), $picture->getData());
				}
			}
			catch (Zend_Media_Flac_Exception $e) {
				// catch any parse errors
			}
			break;

        case 'm4a':
            require_once 'Zend/Media/Iso14496.php';
            try {
                $id3 = new Zend_Media_Iso14496($path);
                $picture = $id3->moov->udta->meta->ilst->covr;
                $mime = ($picture->getFlags() & Zend_Media_Iso14496_Box_Data::JPEG) == Zend_Media_Iso14496_Box_Data::JPEG
                    ? 'image/jpeg'
                    : (
                        ($picture->getFlags() & Zend_Media_Iso14496_Box_Data::PNG) == Zend_Media_Iso14496_Box_Data::PNG
                        ? 'image/png'
                        : null
                    );
                if ($mime) {
                    outImage($mime, $picture->getValue());
                }
            }
            catch (Zend_Media_Iso14496_Exception $e) {
                // catch any parse errors
            }
            break;
	}

	return false;
}

function parseFolder($path) {
	$covers = array(
		'Folder.jpg',
		'folder.jpg',
		'Folder.png',
		'folder.png',
		'Cover.jpg',
		'cover.jpg',
		'Cover.png',
		'cover.png'
	);

	// default cover files
	foreach ($covers as $file) {
		getImage($path . $file);
	}

	// all (other) files
	foreach (glob($path . '*') as $file) {
		if (is_file($file)) {
			getImage($file);
		}
	}
}

/*
 * MAIN
 */

// Get options- cmd line or GET
$options = getopt('p:', array('path:'));
$path = isset($options['p']) ? $options['p'] : (isset($options['path']) ? $options['path'] : null);

if (null === $path) {
	$self = $_SERVER['SCRIPT_NAME'];
	$path = urldecode($_SERVER['REQUEST_URI']);
	if (substr($path, 0, strlen($self)) === $self) {
		// strip script name if called as /coverart.php/path/to/file
		$path = substr($path, strlen($self)+1);
	}
	#$path = '/mnt/' . $path;
	$path = '/var/lib/mpd/music/' . $path;
}

// does file exist and contain image?
getImage($path);

// directory - try all files
if (is_dir($path)) {
	// make sure path ends in /
	if (substr($path, -1) !== '/') {
		$path .= '/';
	}

	parseFolder($path);
}
else {
	// file - try all files in containing folder
	$path = pathinfo($path, PATHINFO_DIRNAME) . '/';

	parseFolder($path);
}

// nothong found -> default cover
header('Location: /images/default-cover.jpg');
