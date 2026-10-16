import gzip
from pathlib import Path


def modern_fileref(rel: str, abs_path: str, rpt: int = 3, size: int = 0) -> str:
    return f"""<FileRef>
								<RelativePathType Value="{rpt}" />
								<RelativePath Value="{rel}" />
								<Path Value="{abs_path}" />
								<Type Value="1" />
								<LivePackName Value="" />
								<LivePackId Value="" />
								<OriginalFileSize Value="{size}" />
								<OriginalCrc Value="12345" />
							</FileRef>"""


def legacy_fileref(dirs: list[str], name: str, has_rel: bool = True, rpt: int = 3, size: int = 0,
                   hint_dirs: list[str] | None = None) -> str:
    rel = "".join(f'<RelativePathElement Id="{i}" Dir="{d}" />' for i, d in enumerate(dirs))
    hint = "".join(f'<RelativePathElement Id="{i}" Dir="{d}" />' for i, d in enumerate(hint_dirs or []))
    return f"""<FileRef>
						<HasRelativePath Value="{'true' if has_rel else 'false'}" />
						<RelativePathType Value="{rpt}" />
						<RelativePath>{rel}</RelativePath>
						<Name Value="{name}" />
						<Type Value="2" />
						<Data />
						<RefersToFolder Value="false" />
						<SearchHint>
							<PathHint>{hint}</PathHint>
							<FileSize Value="{size}" />
							<Crc Value="0" />
							<MaxCrcSize Value="16384" />
							<HasExtendedInfo Value="true" />
						</SearchHint>
						<LivePackName Value="" />
						<LivePackId Value="" />
					</FileRef>"""


def audio_track(name: str, fileref: str, frozen: bool = False, devices: str = "") -> str:
    return f"""<AudioTrack Id="1">
			<Name><EffectiveName Value="{name}" /><UserName Value="{name}" /></Name>
			<DeviceChain><DeviceChain><Devices>{devices}</Devices></DeviceChain>
			<MainSequencer><Sample><ArrangerAutomation><Events>
				<AudioClip Id="0"><SampleRef>{fileref}<LastModDate Value="0" />
					<SourceContext><SourceContext><OriginalFileRef>{modern_fileref("", "/nowhere/browser-origin.wav", 1)}</OriginalFileRef></SourceContext></SourceContext>
				</SampleRef></AudioClip>
			</Events></ArrangerAutomation></Sample></MainSequencer></DeviceChain>
			<Freeze Value="{'true' if frozen else 'false'}" />
		</AudioTrack>"""


def midi_track(name: str, devices: str, frozen: bool = False) -> str:
    return f"""<MidiTrack Id="2">
			<Name><EffectiveName Value="{name}" /><UserName Value="" /></Name>
			<DeviceChain><DeviceChain><Devices>{devices}</Devices></DeviceChain></DeviceChain>
			<Freeze Value="{'true' if frozen else 'false'}" />
		</MidiTrack>"""


def vst3(name: str) -> str:
    return f"""<PluginDevice Id="0"><PluginDesc><Vst3PluginInfo Id="0"><Name Value="{name}" />
		<Uid><Fields.0 Value="1" /><Fields.1 Value="2" /><Fields.2 Value="3" /><Fields.3 Value="4" /></Uid>
		</Vst3PluginInfo></PluginDesc></PluginDevice>"""


def vst2(name: str, path: str) -> str:
    return f"""<PluginDevice Id="0"><PluginDesc><VstPluginInfo Id="0"><Path Value="{path}" />
		<PlugName Value="{name}" /><UniqueId Value="1180193589" /></VstPluginInfo></PluginDesc></PluginDevice>"""


def au(name: str, vendor: str) -> str:
    return f"""<AuPluginDevice Id="0"><PluginDesc><AuPluginInfo Id="0"><Name Value="{name}" />
		<Manufacturer Value="{vendor}" /></AuPluginInfo></PluginDesc></AuPluginDevice>"""


def m4l(fileref: str) -> str:
    return f"""<MxDeviceAudioEffect Id="0"><PatchSlot><Value><MxPatchRef>{fileref}</MxPatchRef></Value></PatchSlot>
		<LastPresetRef><Value><FilePresetRef Id="0">{modern_fileref("", "/Users/x/Library/preset.adv", 1)}</FilePresetRef></Value></LastPresetRef>
		</MxDeviceAudioEffect>"""


def make_set(path: Path, tracks: list[str], creator: str = "Ableton Live 12.1.5") -> Path:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Ableton MajorVersion="5" MinorVersion="12.0_12117" SchemaChangeCount="3" Creator="{creator}" Revision="abc">
	<LiveSet>
		<Tracks>
		{''.join(tracks)}
		</Tracks>
		<MainTrack><Name><EffectiveName Value="Main" /></Name><DeviceChain><Devices /></DeviceChain></MainTrack>
	</LiveSet>
</Ableton>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(xml.encode("utf-8")))
    return path


def read_set_xml(path: Path) -> str:
    return gzip.decompress(path.read_bytes()).decode("utf-8")
