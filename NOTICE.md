# Notices

Auralyn OBS source prototype: original code is licensed under GPL-3.0-only.
The full license text is in LICENSE.

This plugin uses OBS Studio's libobs API. The tested SDK source is OBS Studio
32.2.2, commit ba2f32bdf791005443988a4955e963663e16b1ed, from
https://github.com/obsproject/obs-studio . The libobs headers carry their own
copyright notices and GPL version 2 or later terms; retain those notices.
The GPLv3 option is selected for this combination. Refer to the pinned OBS
source's COPYING and individual file notices for third-party conditions.

The local build reads the installed OBS runtime and generates an import
library. OBS runtime binaries, SDK headers, and the generated import library
are not redistributed in this repository. A future binary release must
provide the corresponding source and build provenance required for that
release, rather than relying solely on the Auralyn application source ZIP.

OBS Studio is a separate project. This prototype is not an OBS Project
release or endorsement.
