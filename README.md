# CamTuner

Adjusts a basic webcam (zoom, pan, rotation, flip, exposure, brightness, contrast,
gamma, saturation, temperature, tint, sharpness) and sends the result to a virtual
camera. Chrome and other apps select **OBS Virtual Camera** and see the adjusted video.

## One-time setup on the Windows PC

1. Install OBS Studio from https://obsproject.com
2. Open OBS, click **Start Virtual Camera**, then **Stop Virtual Camera**, then close OBS.
   You never need to open OBS again.

## Getting the .exe (no building on the Mac)

1. Put this folder in a GitHub repository (keep the `.github` folder).
2. Every push builds the exe automatically on GitHub's Windows machines.
3. On the Windows PC, open the repo on github.com, go to **Actions**, open the latest
   successful run, and download **CamTuner-exe** (a zip containing CamTuner.exe).

Alternative: copy this folder to the Windows PC and double-click `build_windows.bat`.

## Using it

1. Run CamTuner.exe and pick your real webcam at the top.
2. Adjust the sliders. Drag the preview to pan, scroll on it to zoom.
3. In Chrome (or any app), choose **OBS Virtual Camera** as the camera.
4. Minimize CamTuner to send it to the system tray. Right-click the tray icon for
   **Open CamTuner** and **Quit**. Quitting asks for confirmation and stops the adjusted video.
5. Tick **Start with Windows** on the Help tab to have it start in the tray at login.

## Webcam on only when needed

With **Webcam on only while an app is using it** ticked (Help tab, on by default), the real
webcam is off until an app opens OBS Virtual Camera. It switches off again 5 seconds after
the last app lets go. While the CamTuner window is open, the webcam stays on so the preview
works; minimize to the tray to let it switch off.

The Help tab shows which apps CamTuner currently sees using the virtual camera. If the
webcam stays on with no app in use, that list names the app keeping it on.

Settings and a log file are stored in `%APPDATA%\CamTuner`.

## Notes

- Your webcam has no hardware controls, so exposure and the other light controls are
  applied in software to the picture.
- CamTuner uses your real webcam, so other apps should use OBS Virtual Camera, not the
  real webcam, while CamTuner is running.
