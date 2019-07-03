# dicom operations
import os
import os.path as op
import logging
from collections import OrderedDict
import tarfile

from .external.pydicom import dcm
from .utils import load_json, get_typed_attr, set_readonly, SeqInfo

import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    # suppress warning
    import nibabel.nicom.dicomwrappers as dw

lgr = logging.getLogger(__name__)


def create_seqinfo(mw, series_files, series_id):
    """Generate sequence info

    Parameters
    ----------
    mw: MosaicWrapper
    series_files: list
    series_id: str
    """
    dcminfo = mw.dcm_data
    accession_number = dcminfo.get('AccessionNumber')

    # TODO: do not group echoes by default
    size = list(mw.image_shape) + [len(series_files)]
    fls = size[-1]
    if len(size) < 4:
        size.append(1)

    # parse DICOM for seqinfo fields
    TR = get_typed_attr(dcminfo, "RepetitionTime", float, -1000) / 1000
    TE = get_typed_attr(dcminfo, "EchoTime", float, -1)
    refphys = get_typed_attr(dcminfo, "ReferringPhysicianName", str, "")
    image_type = get_typed_attr(dcminfo, "ImageType", tuple, "")
    is_moco = 'MOCO' in image_type
    series_desc = get_typed_attr(dcminfo, "SeriesDescription", str, "")

    if dcminfo.get([0x18, 0x24]):
        # GE and Philips
        sequence_name = dcminfo[0x18, 0x24].value
    elif dcminfo.get([0x19, 0x109c]):
        # Siemens
        sequence_name = dcminfo[0x19, 0x109c].value
    else:
        sequence_name = ""

    # TO CONSIDER:
    # dcminfo.AccessionNumber
    # len(dcminfo.ReferencedImageSequence)
    # len(dcminfo.SourceImageSequence)
    # FOR demographics
    seqinfo = SeqInfo(
        fls,
        op.basename(series_files[0]),
        series_id,
        op.basename(op.dirname(series_files[0])),
        size[0], size[1], size[2], size[3],
        TR, TE,
        dcminfo.ProtocolName,
        is_moco,
        'derived' in [x.lower() for x in dcminfo.get('ImageType', [])],
        dcminfo.get('PatientID'),
        dcminfo.get('StudyDescription'),
        refphys,
        series_desc,
        sequence_name,
        image_type,
        accession_number,
        # For demographics to populate BIDS participants.tsv
        dcminfo.get('PatientAge'),
        dcminfo.get('PatientSex'),
        dcminfo.get('AcquisitionDate'),
        dcminfo.get('SeriesInstanceUID')
    )
    return seqinfo


def validate_dicom(fl, dcmfilter):
    """
    Parse DICOM attributes. Returns None if not valid.
    """
    mw = dw.wrapper_from_file(fl, force=True, stop_before_pixels=True)
    # clean series signature
    for sig in ('iop', 'ICE_Dims', 'SequenceName'):
        try:
            del mw.series_signature[sig]
        except Exception:
            pass
    try:
        series_id = (
            int(mw.dcm_data.SeriesNumber), mw.dcm_data.ProtocolName
        )
    except AttributeError as e:
        lgr.warning(
            'Ignoring %s since not quite a "normal" DICOM: %s', fl, e
        )
        return None
    if dcmfilter is not None and dcmfilter(mw.dcm_data):
        lgr.warning("Ignoring %s because of DICOM filter", fl)
        return None
    if mw.dcm_data[0x0008, 0x0016].repval in (
        'Raw Data Storage',
        'GrayscaleSoftcopyPresentationStateStorage'
    ):
        return None
    try:
        file_studyUID = mw.dcm_data.StudyInstanceUID
    except AttributeError:
        lgr.info("File {} is missing any StudyInstanceUID".format(fl))
        file_studyUID = None
    return mw, series_id, file_studyUID


def group_dicoms_into_seqinfos(files, file_filter, dcmfilter, grouping):
    """Process list of dicoms and return seqinfo and file group
    `seqinfo` contains per-sequence extract of fields from DICOMs which
    will be later provided into heuristics to decide on filenames
    Parameters
    ----------
    files : list of str
      List of files to consider
    file_filter : callable, optional
      Applied to each item of filenames. Should return True if file needs to be
      kept, False otherwise.
    dcmfilter : callable, optional
      If called on dcm_data and returns True, it is used to set series_id
    grouping : str
      Possible groupings: studyUID, accession_number, all, file, custom


    Returns
    -------
    seqinfo : list of list
      `seqinfo` is a list of info entries per each sequence (some entry
      there defines a key for `filegrp`)
    filegrp : dict
      `filegrp` is a dictionary with files groupped per each sequence
    """
    allowed_groupings = ['studyUID', 'accession_number', 'all', 'file', 'custom']
    if grouping not in allowed_groupings:
        raise ValueError('I do not know how to group by {0}'.format(grouping))
    per_studyUID = grouping == 'studyUID'
    # per_accession_number = grouping == 'accession_number'
    lgr.info("Analyzing %d dicoms", len(files))

    groups = [[], []]
    mwgroup = []
    studyUID = None

    if file_filter:
        nfl_before = len(files)
        files = list(filter(file_filter, files))
        nfl_after = len(files)
        lgr.info('Filtering out {0} dicoms based on their filename'.format(
            nfl_before-nfl_after))

    for filename in files:
        # TODO after getting a regression test check if the same behavior
        #      with stop_before_pixels=True

        mwinfo = validate_dicom(filename, dcmfilter)
        if mwinfo is None:
            continue
        mw, series_id, file_studyUID = mwinfo
        if per_studyUID:
            series_id = series_id + (file_studyUID,)

        if not per_studyUID:
            # verify that we are working with a single study
            if studyUID is None:
                studyUID = file_studyUID
            elif grouping not in ['accession_number', 'all']:
                assert studyUID == file_studyUID, (
                    "Conflicting study identifiers found [{}, {}].".format(
                        studyUID, file_studyUID)
                )
        # Workaround for protocol name in private siemens csa header
        if not getattr(mw.dcm_data, 'ProtocolName', '').strip():
            mw.dcm_data.ProtocolName = parse_private_csa_header(
                mw.dcm_data, 'ProtocolName', 'tProtocolName'
            ) if mw.is_csa else ''

        ingrp = False
        # check if same series was already converted
        for idx in range(len(mwgroup)):
            if mw.is_same_series(mwgroup[idx]):
                if grouping != 'all':
                    assert (
                        mwgroup[idx].dcm_data.get('StudyInstanceUID') == file_studyUID
                    ), "Same series found for multiple different studies"
                ingrp = True
                series_id = (
                    mwgroup[idx].dcm_data.SeriesNumber,
                    mwgroup[idx].dcm_data.ProtocolName
                )
                if per_studyUID:
                    series_id = series_id + (file_studyUID,)
                groups[0].append(series_id)
                groups[1].append(idx)

        if not ingrp:
            mwgroup.append(mw)
            groups[0].append(series_id)
            groups[1].append(len(mwgroup) - 1)

    group_map = dict(zip(groups[0], groups[1]))

    seqinfos = OrderedDict()
    # for the next line to make any sense the series_id needs to
    # be sortable in a way that preserves the series order
    for series_id, mwidx in sorted(group_map.items()):
        mw = mwgroup[mwidx]
        series_files = [files[i] for i, s in enumerate(groups[0]) if s == series_id]
        if per_studyUID:
            studyUID = series_id[2]
            series_id = series_id[:2]
        series_id = '-'.join(map(str, series_id))
        if mw.image_shape is None:
            # this whole thing has no image data (maybe just PSg DICOMs)
            # nothing to see here, just move on
            continue
        seqinfo = create_seqinfo(mw, series_files, series_id)

        if per_studyUID:
            key = studyUID.split('.')[-1]
        elif grouping == 'accession_number':
            key = accession_number = mw.dcm_data.get("AccessionNumber")
        else:
            key = ''
        lgr.debug("%30s %30s %27s %27s %5s nref=%-2d nsrc=%-2d %s" % (
            key,
            seqinfo.series_id,
            seqinfo.series_desc,
            mw.dcm_data.ProtocolName,
            seqinfo.is_derived,
            len(mw.dcm_data.get('ReferencedImageSequence', '')),
            len(mw.dcm_data.get('SourceImageSequence', '')),
            seqinfo.image_type
        ))

        if per_studyUID:
            if studyUID not in seqinfos:
                seqinfos[studyUID] = OrderedDict()
            seqinfos[studyUID][seqinfo] = series_files
        elif grouping == 'accession_number':
            if accession_number not in seqinfos:
                seqinfos[accession_number] = OrderedDict()
            seqinfos[accession_number][seqinfo] = series_files
        elif grouping == 'all':
            if not seqinfos.get('all'):
                seqinfos['all'] = OrderedDict()
            seqinfos['all'][seqinfo] = series_files
        else:
            seqinfos[seqinfo] = series_files

    if per_studyUID:
        lgr.info("Generated sequence info for %d studies with %d entries total",
                 len(seqinfos), sum(map(len, seqinfos.values())))
    elif grouping == 'accession_number':
        lgr.info("Generated sequence info for %d accession numbers with %d "
                 "entries total", len(seqinfos), sum(map(len, seqinfos.values())))
    else:
        lgr.info("Generated sequence info with %d entries", len(seqinfos))
    return seqinfos


def get_dicom_series_time(dicom_list):
    """Get time in seconds since epoch from dicom series date and time
    Primarily to be used for reproducible time stamping
    """
    import time
    import calendar

    dicom = dcm.read_file(dicom_list[0], stop_before_pixels=True, force=True)
    dcm_date = dicom.SeriesDate  # YYYYMMDD
    dcm_time = dicom.SeriesTime  # HHMMSS.MICROSEC
    dicom_time_str = dcm_date + dcm_time.split('.', 1)[0]  # YYYYMMDDHHMMSS
    # convert to epoch
    return calendar.timegm(time.strptime(dicom_time_str, '%Y%m%d%H%M%S'))


def compress_dicoms(dicom_list, out_prefix, tempdirs, overwrite):
    """Archives DICOMs into a tarball

    Also tries to do it reproducibly, so takes the date for files
    and target tarball based on the series time (within the first file)

    Parameters
    ----------
    dicom_list : list of str
      list of dicom files
    out_prefix : str
      output path prefix, including the portion of the output file name
      before .dicom.tgz suffix
    tempdirs : object
      TempDirs object to handle multiple tmpdirs
    overwrite : bool
      Overwrite existing tarfiles

    Returns
    -------
    filename : str
      Result tarball
    """

    tmpdir = tempdirs(prefix='dicomtar')
    outtar = out_prefix + '.dicom.tgz'

    if op.exists(outtar) and not overwrite:
        lgr.info("File {} already exists, will not overwrite".format(outtar))
        return
    # tarfile encodes current time.time inside making those non-reproducible
    # so we should choose which date to use.
    # Solution from DataLad although ugly enough:

    dicom_list = sorted(dicom_list)
    dcm_time = get_dicom_series_time(dicom_list)

    def _assign_dicom_time(ti):
        # Reset the date to match the one of the last commit, not from the
        # filesystem since git doesn't track those at all
        ti.mtime = dcm_time
        return ti

    # poor man mocking since can't rely on having mock
    try:
        import time
        _old_time = time.time
        time.time = lambda: dcm_time
        if op.lexists(outtar):
            os.unlink(outtar)
        with tarfile.open(outtar, 'w:gz', dereference=True) as tar:
            for filename in dicom_list:
                outfile = op.join(tmpdir, op.basename(filename))
                if not op.islink(outfile):
                    os.symlink(op.realpath(filename), outfile)
                # place into archive stripping any lead directories and
                # adding the one corresponding to prefix
                tar.add(outfile,
                        arcname=op.join(op.basename(out_prefix),
                                        op.basename(outfile)),
                        recursive=False,
                        filter=_assign_dicom_time)
    finally:
        time.time = _old_time
        tempdirs.rmtree(tmpdir)

    return outtar


def embed_nifti(dcmfiles, niftifile, infofile, bids_info, min_meta):
    """

    If `niftifile` doesn't exist, it gets created out of the `dcmfiles` stack,
    and json representation of its meta_ext is returned (bug since should return
    both niftifile and infofile?)

    if `niftifile` exists, its affine's orientation information is used while
    establishing new `NiftiImage` out of dicom stack and together with `bids_info`
    (if provided) is dumped into json `infofile`

    Parameters
    ----------
    dcmfiles
    niftifile
    infofile
    bids_info
    min_meta

    Returns
    -------
    niftifile, infofile

    """
    # imports for nipype
    import nibabel as nb
    import os
    import os.path as op
    import json
    import re

    if not min_meta:
        from heudiconv.external.dcmstack import ds
        stack = ds.parse_and_stack(dcmfiles, force=True).values()
        if len(stack) > 1:
            raise ValueError('Found multiple series')
        # may be odict now - iter to be safe
        stack = next(iter(stack))

        #Create the nifti image using the data array
        if not op.exists(niftifile):
            nifti_image = stack.to_nifti(embed_meta=True)
            nifti_image.to_filename(niftifile)
            return ds.NiftiWrapper(nifti_image).meta_ext.to_json()

        orig_nii = nb.load(niftifile)
        aff = orig_nii.affine
        ornt = nb.orientations.io_orientation(aff)
        axcodes = nb.orientations.ornt2axcodes(ornt)
        new_nii = stack.to_nifti(voxel_order=''.join(axcodes), embed_meta=True)
        meta = ds.NiftiWrapper(new_nii).meta_ext.to_json()

    meta_info = None if min_meta else json.loads(meta)

    if bids_info:

        if min_meta:
            meta_info = bids_info
        else:
            # make nice with python 3 - same behavior?
            meta_info = meta_info.copy()
            meta_info.update(bids_info)
            # meta_info = dict(meta_info.items() + bids_info.items())
        try:
            meta_info['TaskName'] = (re.search('(?<=_task-)\w+',
                                               op.basename(infofile))
                                     .group(0).split('_')[0])
        except AttributeError:
            pass
    # write to outfile
    with open(infofile, 'wt') as fp:
        json.dump(meta_info, fp, indent=3, sort_keys=True)

    return niftifile, infofile


def embed_metadata_from_dicoms(bids_options, item_dicoms, outname, outname_bids,
                               prov_file, scaninfo, tempdirs, with_prov,
                               min_meta):
    """
    Enhance sidecar information file with more information from DICOMs

    Parameters
    ----------
    bids_options
    item_dicoms
    outname
    outname_bids
    prov_file
    scaninfo
    tempdirs
    with_prov
    min_meta

    Returns
    -------

    """
    from nipype import Node, Function
    tmpdir = tempdirs(prefix='embedmeta')

    # We need to assure that paths are absolute if they are relative
    item_dicoms = list(map(op.abspath, item_dicoms))

    embedfunc = Node(Function(input_names=['dcmfiles', 'niftifile', 'infofile',
                                           'bids_info', 'min_meta'],
                              output_names=['outfile', 'meta'],
                              function=embed_nifti),
                     name='embedder')
    embedfunc.inputs.dcmfiles = item_dicoms
    embedfunc.inputs.niftifile = op.abspath(outname)
    embedfunc.inputs.infofile = op.abspath(scaninfo)
    embedfunc.inputs.min_meta = min_meta
    embedfunc.inputs.bids_info = load_json(op.abspath(outname_bids)) if (bids_options is not None) else None
    embedfunc.base_dir = tmpdir
    cwd = os.getcwd()

    lgr.debug("Embedding into %s based on dicoms[0]=%s for nifti %s",
              scaninfo, item_dicoms[0], outname)
    try:
        if op.lexists(scaninfo):
            # TODO: handle annexed file case
            if not op.islink(scaninfo):
                set_readonly(scaninfo, False)
        res = embedfunc.run()
        set_readonly(scaninfo)
        if with_prov:
            g = res.provenance.rdf()
            g.parse(prov_file,
                    format='turtle')
            g.serialize(prov_file, format='turtle')
            set_readonly(prov_file)
    except Exception as exc:
        lgr.error("Embedding failed: %s", str(exc))
        os.chdir(cwd)

def parse_private_csa_header(dcm_data, public_attr, private_attr, default=None):
    """
    Parses CSA header in cases where value is not defined publicly

    Parameters
    ----------
    dcm_data : pydicom Dataset object
        DICOM metadata
    public_attr : string
        non-private DICOM attribute
    private_attr : string
        private DICOM attribute
    default (optional)
        default value if private_attr not found

    Returns
    -------
    val (default: empty string)
        private attribute value or default
    """
    # TODO: provide mapping to private_attr from public_attr
    from nibabel.nicom import csareader
    import dcmstack.extract as dsextract
    try:
        # TODO: test with attr besides ProtocolName
        csastr = csareader.get_csa_header(dcm_data, 'series')['tags']['MrPhoenixProtocol']['items'][0]
        csastr = csastr.replace("### ASCCONV BEGIN", "### ASCCONV BEGIN ### ")
        parsedhdr = dsextract.parse_phoenix_prot('MrPhoenixProtocol', csastr)
        val = parsedhdr[private_attr].replace(' ', '')
    except Exception as e:
        lgr.debug("Failed to parse CSA header: %s", str(e))
        val = default or ""
    return val
