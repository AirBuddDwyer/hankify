HANKIFY FOR WINDOWS
===================

Drop a song in, pick a voice, and get back the same song sung in that voice (an MP3, or an MP4 with a
picture). Under the hood: the vocals are separated from the music (UVR's MDX23C model), converted to the chosen voice
(RVC), cleaned up and mixed back over the instrumental.

There is no .exe in here. Everything is plain-text scripts (.bat, .ps1, .py) you can open in Notepad and read before
running. The installer downloads the actual programs from their official sources (listed at the top of
windows\install.ps1) into this folder only: nothing is installed system-wide and no admin rights are needed.
To uninstall, delete the folder.
(On Linux, use ./install_linux.sh instead: see README.md, "Linux (quick install)".)


WHAT YOU NEED
- Windows 10 or 11, 64-bit
- About 15 GB of free disk space
- An NVIDIA graphics card is strongly recommended (any GTX 10-series or newer, 4 GB is enough).
  Without one it still works, on the CPU, but a song takes many minutes instead of ~5.
- Internet for the install (it downloads ~5 GB once)


INSTALL (once)
1. Right-click the zip > Properties > tick "Unblock" (if that box is there) > OK. Then extract it.
2. Move the extracted folder somewhere short, like  C:\Hankify  (not Downloads, Desktop or anything in OneDrive:
   long paths break the install).
3. Double-click  "Install Hankify.bat".
   If Windows shows "Windows protected your PC", click "More info" > "Run anyway".
4. Wait. The first install takes 10-30 minutes depending on your internet. It ends with a list of checks that
   should all say "ok", then "All done".
   If something fails, run it again (it picks up where it stopped). Still failing? Send a screenshot of the window.


USE IT
1. Double-click  "Hankify.bat". A black window opens (leave it open; errors show up there) and then the Hankify window.
2. Pick a voice (no voices yet? see GETTING VOICES below), drag songs (MP3/FLAC/M4A/WAV/MP4) into the
   window, press Start Queue.
3. Finished songs land in your Music folder, under  Music\Hankify  ("Show File" opens it).
   Tick "Also make MP4" to get a video with a picture, for posting.

Tips
- Pitch: leave it at 0 for most songs. A female singer through a male voice may sound better at -12 (one octave down).
- "Preserve backing vocals (convert lead only)" converts only the lead singer and keeps the original backup singers.
- The first song after starting is a bit slower (models loading).

SEVERAL VOICES IN ONE SONG (Hankiplier)
Double-click  "Hankiplier.bat". Drop in one song, paste its lyrics (or a Genius link), give each singer a voice,
press Hankiply!. It works out who sings which line and builds one song with every part in its own voice.


GETTING VOICES (do this first: Hankify comes with NO voices)
A "voice" is an RVC voice model someone trained on a character's or singer's recordings. People share thousands
of them for free. Each one is a .pth file (the voice) and usually an .index file (helps the accent and
character), normally packed together in a .zip.

1. FIND ONE. Google the character's name plus "RVC model", for example:
       <character name> RVC model
   The results almost always lead to  https://voice-models.com  (or click "Find Voices Online" in Hankify).
   On voice-models.com, type the character's name in the search box. You'll often get several versions of
   the same character. Pick one that:
   - says RVC (ideally "RVC v2"),
   - has lots of downloads or likes,
   - has a high epoch count (300+), if it's listed,
   - mentions singing, if you have the choice ("singing" models handle songs better than "speaking" ones).
   Other sites (huggingface.co, weights.gg) work too, as long as you end up with a .zip or a .pth file.

2. DOWNLOAD IT. Click Download. You get a .zip in your Downloads folder. Don't unzip it.

3. IMPORT IT. In Hankify, click  "Import Voice..."  (under the Voice list) and pick the .zip.
   Or just drag the .zip onto Hankify's drop box, the same place songs go.
   Hankify takes the voice out of the zip, puts the files where they need to go and selects the new voice.
   Done: it's in the Voice list from now on. You can import several zips at once.

Good to know
- If a site gives you a .pth and an .index file instead of a zip, import both together
  (select both in the Import Voice window, or drag both at once).
- Files called G_1234.pth / D_1234.pth or trained_*.index are training leftovers; Hankify ignores them.
- "No RVC voice inside": that zip holds a different kind of model (so-vits-svc, Diff-SVC, ...).
  Look for another version that says RVC.
- Importing the same zip twice does nothing. A different voice with the same name gets " (2)" added.
- Try a few versions of a character: some sound much better than others on songs.
- Another way: put the downloaded .zip files in Hankify's  voices  folder and run "Install Hankify.bat"
  again. It imports everything in there.
- Removing a voice: delete its .pth from  RVC\assets\weights  (and its .index from  RVC\assets\indices).
- A voice file can run code when it's loaded: only download voices from the well-known sites above.
- Voice models are made and shared by other people; this package doesn't include, endorse or vouch for any of
  them. You are responsible for having the right to use a voice and the songs you put through it.


IF SOMETHING GOES WRONG
- A song failed: the reason is in the Hankify window, and in  hankify\errors.log.
- Hankify.bat closes right away: run "Install Hankify.bat" again and check that every line says ok.
