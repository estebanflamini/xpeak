# Contributing to xpeak

If you want to contribute to this project, you're welcome! This is just a first basic version, and there are several contributions to be made:
- The current version is only meant to run under Linux, and it was only tested in Kubuntu 14.04. It should work in other Linux distros (and perhaps Mac); it surely won't work in Windows right now. I marked with a TODO comment several parts of the code where there are known portability issues; there can be others I neglected. You can contribute to this project by porting it.
- The current version only supports **espeak**. Support for other speech-synthesis engines might be desirable as well.
- The program's interface is written in English. You are welcome to localize it. The program uses ``gettext`` for all of its localizable strings. Please refer to ``gettext`` in _The Python Library Reference_.
- The current segmentation (sentence splitting) companion script ``xplit.py`` is very basic. I needed a quick sentence-splitting solution, and once I got what I needed, I focused on developing **xpeak**. You might want to improve the script and the simple XML-like language I devised for defining splitting rules. An obvious improvement would be to have ``xplit.py`` understand the SRX format (SRX is a standard used in translation for defining _text segmentation rules_, that is, text splitting; I happen not to use SRX in my current workflows, so I didn't try to make **xplit** understand it; you might want to improve this).
- Currently, up to seven substitution files can be used and their order of application is fixed. Some flexibilization might be desirable.
- A feature to read the key bindings from a configuration file would be useful, and it is easy to implement.
- New features might be added, existing ones can be improved.
