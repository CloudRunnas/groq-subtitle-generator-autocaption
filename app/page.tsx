'use client'
import React, { useState, useCallback, useRef, memo, useEffect } from 'react'
import { Upload, Play, Download, Globe, Clock, FileText, CheckCircle, AlertCircle, Eye } from 'lucide-react'

//types for managing the status and file info
interface JobStatus {
  job_id: string
  status: string
  progress: number
  message: string
  output_path?: string
  subtitle_path?: string
  karaoke?: boolean
  has_word_timings?: boolean
}

interface WordTiming {
  word: string
  start: number
  end: number
  score?: number | null
  cue_index?: number
  aligned?: boolean
}

interface FileInfo {
  filename: string
  size: number
  duration?: number
  format?: string
  resolution?: string
  fps?: number
  mode?: 'generate' | 'burn' | 'burn_words'
  srt_filename?: string | null
  word_timings_filename?: string | null
  word_count?: number
}

interface FileInfoCardProps {
  icon: React.ComponentType<{ className?: string }>
  iconColor: string
  label: string
  value: string
  title?: string
}

interface FileInfoSectionProps {
  fileInfo: FileInfo
  title: string
  icon: React.ComponentType<{ className?: string }>
  gradientFrom: string
  gradientTo: string
  borderColor: string
  iconColor: string
  formatFileSize: (bytes: number) => string
  formatDuration: (seconds: number) => string
  getFileExtension: (filename: string) => string
  getAspectRatio: (resolution: string) => string
  getEstimatedBitrate: (size: number, duration: number) => string
}


//list of supported languages by qwen3-32b. if you change the model, please update this list.
const SUPPORTED_LANGUAGES = {
  'en': 'English',
  'es': 'Spanish',
  'fr': 'French',
  'de': 'German',
  'it': 'Italian',
  'pt': 'Portuguese',
  'ru': 'Russian',
  'ja': 'Japanese',
  'ko': 'Korean',
  'zh': 'Chinese',
  'ar': 'Arabic',
  'hi': 'Hindi',
  'th': 'Thai',
  'vi': 'Vietnamese',
  'nl': 'Dutch',
  'sv': 'Swedish',
  'no': 'Norwegian',
  'da': 'Danish',
  'fi': 'Finnish',
  'pl': 'Polish',
  'tr': 'Turkish',
  'cs': 'Czech',
  'hu': 'Hungarian',
  'ro': 'Romanian',
  'bg': 'Bulgarian',
  'hr': 'Croatian',
  'sk': 'Slovak',
  'sl': 'Slovenian',
  'et': 'Estonian',
  'lv': 'Latvian',
  'lt': 'Lithuanian',
  'mt': 'Maltese',
  'ga': 'Irish',
  'cy': 'Welsh',
  'eu': 'Basque',
  'ca': 'Catalan',
  'gl': 'Galician',
  'is': 'Icelandic',
  'mk': 'Macedonian',
  'sq': 'Albanian',
  'be': 'Belarusian',
  'uk': 'Ukrainian',
  'he': 'Hebrew',
  'fa': 'Persian',
  'ur': 'Urdu',
  'bn': 'Bengali',
  'ta': 'Tamil',
  'te': 'Telugu',
  'ml': 'Malayalam',
  'kn': 'Kannada',
  'gu': 'Gujarati',
  'mr': 'Marathi',
  'ne': 'Nepali',
  'si': 'Sinhala',
  'my': 'Burmese',
  'km': 'Khmer',
  'lo': 'Lao',
  'ka': 'Georgian',
  'am': 'Amharic',
  'sw': 'Swahili',
  'zu': 'Zulu',
  'af': 'Afrikaans',
  'ms': 'Malay',
  'tl': 'Filipino',
  'id': 'Indonesian'
}

//info card component
const FileInfoCard = memo<FileInfoCardProps>(({ icon: Icon, iconColor, label, value, title }) => (
  <div className="bg-white rounded-lg p-4 border border-gray-200 shadow-sm">
    <div className="text-center">
      <div className={`w-8 h-8 bg-${iconColor}-100 rounded-full flex items-center justify-center mx-auto mb-2`}>
        <Icon className={`w-4 h-4 text-${iconColor}-600`} />
      </div>
      <span className="text-gray-500 block text-sm mb-1">{label}</span>
      <span 
        className="text-gray-900 font-medium text-sm block truncate max-w-full px-1" 
        title={title || value}
      >
        {value}
      </span>
    </div>
  </div>
))

FileInfoCard.displayName = 'FileInfoCard'

//file info section component to display video info
const FileInfoSection = memo<FileInfoSectionProps>(({
  fileInfo,
  title,
  icon: TitleIcon,
  gradientFrom,
  gradientTo,
  borderColor,
  iconColor,
  formatFileSize,
  formatDuration,
  getFileExtension,
  getAspectRatio,
  getEstimatedBitrate
}) => {
  const fileInfoCards = [
    {
      icon: FileText,
      iconColor: 'blue',
      label: 'Filename',
      value: fileInfo.filename,
      title: fileInfo.filename
    },
    {
      icon: Play,
      iconColor: 'purple',
      label: 'File Type',
      value: getFileExtension(fileInfo.filename)
    },
    {
      icon: Globe,
      iconColor: 'green',
      label: 'Size',
      value: formatFileSize(fileInfo.size)
    },
    ...(fileInfo.duration ? [{
      icon: Clock,
      iconColor: 'orange',
      label: 'Duration',
      value: formatDuration(fileInfo.duration)
    }] : []),
    ...(fileInfo.resolution ? [{
      icon: Eye,
      iconColor: 'indigo',
      label: 'Resolution',
      value: fileInfo.resolution
    }, {
      icon: Eye,
      iconColor: 'pink',
      label: 'Aspect Ratio',
      value: getAspectRatio(fileInfo.resolution)
    }] : []),
    ...(fileInfo.fps ? [{
      icon: Play,
      iconColor: 'yellow',
      label: 'Frame Rate',
      value: `${fileInfo.fps.toFixed(1)} FPS`
    }] : []),
    ...(fileInfo.duration ? [{
      icon: Upload,
      iconColor: 'red',
      label: 'Est. Bitrate',
      value: getEstimatedBitrate(fileInfo.size, fileInfo.duration)
    }] : []),
    ...(fileInfo.format ? [{
      icon: FileText,
      iconColor: 'teal',
      label: 'Format',
      value: fileInfo.format
    }] : [])
  ]

  return (
    <div className={`bg-gradient-to-r from-${gradientFrom} to-${gradientTo} rounded-xl p-6 border border-${borderColor}`}>
      <h3 className="font-semibold text-gray-900 mb-6 text-lg flex items-center">
        <TitleIcon className={`w-5 h-5 mr-2 text-${iconColor}-600`} />
        {title}
      </h3>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {fileInfoCards.map((card, index) => (
          <FileInfoCard key={index} {...card} />
        ))}
      </div>
    </div>
  )
})

FileInfoSection.displayName = 'FileInfoSection'

//main component with all the state and logic for video subtitle generation
export default function VideoSubtitleGenerator() {
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [selectedSrtFile, setSelectedSrtFile] = useState<File | null>(null)
  const [selectedWordTimingsFile, setSelectedWordTimingsFile] = useState<File | null>(null)
  const [burnSubtitles, setBurnSubtitles] = useState(false)
  const [burnFromWordTimings, setBurnFromWordTimings] = useState(false)
  const [karaokeEnabled, setKaraokeEnabled] = useState(true)
  const [targetLanguage, setTargetLanguage] = useState<string>('en')
  const [sourceLanguage, setSourceLanguage] = useState<string>('')
  const [jobId, setJobId] = useState<string>('')
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null)
  const [fileInfo, setFileInfo] = useState<FileInfo | null>(null)
  const [isUploading, setIsUploading] = useState(false)
  const [isProcessing, setIsProcessing] = useState(false)
  const [error, setError] = useState<string>('')
  const [dragActive, setDragActive] = useState(false)
  const [showVideoPreview, setShowVideoPreview] = useState(false)
  const [transcription, setTranscription] = useState<any>(null)
  const [isEditingTranscription, setIsEditingTranscription] = useState(false)
  const [wordTimings, setWordTimings] = useState<WordTiming[]>([])
  const [karaokeWindowSize, setKaraokeWindowSize] = useState(5)
  const [activeWordIndex, setActiveWordIndex] = useState(-1)
  const [overlayLayout, setOverlayLayout] = useState({
    width_pct: 80,
    left_pct: 10,
    bottom_pct: 5,
    height_pct: 15,
  })
  const fileInputRef = useRef<HTMLInputElement>(null)
  const srtInputRef = useRef<HTMLInputElement>(null)
  const wordTimingsInputRef = useRef<HTMLInputElement>(null)
  const previewVideoRef = useRef<HTMLVideoElement>(null)

  const formatFileSize = useCallback((bytes: number): string => {
    if (bytes === 0) return '0 Bytes'
    const k = 1024
    const sizes = ['Bytes', 'KB', 'MB', 'GB']
    const i = Math.floor(Math.log(bytes) / Math.log(k))
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
  }, [])

  const formatDuration = useCallback((seconds: number): string => {
    const mins = Math.floor(seconds / 60)
    const secs = Math.floor(seconds % 60)
    return `${mins}:${secs.toString().padStart(2, '0')}`
  }, [])

  const getAspectRatio = useCallback((resolution: string): string => {
    if (!resolution) return 'Unknown'
    const [width, height] = resolution.split('x').map(Number)
    if (!width || !height) return 'Unknown'
    
    const gcd = (a: number, b: number): number => b === 0 ? a : gcd(b, a % b)
    const divisor = gcd(width, height)
    return `${width / divisor}:${height / divisor}`
  }, [])

  const getEstimatedBitrate = useCallback((size: number, duration: number): string => {
    if (!duration || duration === 0) return 'Unknown'
    const bitrate = (size * 8) / duration / 1000 // convert to kbps
    if (bitrate > 1000) {
      return `${(bitrate / 1000).toFixed(1)} Mbps`
    }
    return `${bitrate.toFixed(0)} kbps`
  }, [])

  const getFileExtension = useCallback((filename: string): string => {
    return filename.split('.').pop()?.toUpperCase() || 'Unknown'
  }, [])

  const handleDrag = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true)
    } else if (e.type === 'dragleave') {
      setDragActive(false)
    }
  }, [])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)
    
    const files = Array.from(e.dataTransfer.files)
    if (files.length > 0) {
      const file = files[0]
      if (file.type.startsWith('video/')) {
        setSelectedFile(file)
      } else {
        setError('Please select a video file')
      }
    }
  }, [])

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) {
      setSelectedFile(file)
      setError('')
    }
  }, [])

  const handleSrtSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return

    if (!file.name.toLowerCase().endsWith('.srt')) {
      setError('Please select an .srt subtitle file')
      setSelectedSrtFile(null)
      if (srtInputRef.current) {
        srtInputRef.current.value = ''
      }
      return
    }

    setSelectedSrtFile(file)
    setError('')
  }, [])

  const handleWordTimingsSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return

    if (!file.name.toLowerCase().endsWith('.json')) {
      setError('Please select a word timings .json file')
      setSelectedWordTimingsFile(null)
      if (wordTimingsInputRef.current) {
        wordTimingsInputRef.current.value = ''
      }
      return
    }

    setSelectedWordTimingsFile(file)
    setError('')
  }, [])

  const uploadVideo = useCallback(async () => {
    if (!selectedFile) return

    if (burnFromWordTimings && !selectedWordTimingsFile) {
      setError('Please select a word timings JSON file')
      return
    }

    if (burnSubtitles && !burnFromWordTimings && !selectedSrtFile) {
      setError('Please select an SRT file to burn into the video')
      return
    }

    if (burnSubtitles && !burnFromWordTimings && !sourceLanguage) {
      setError('Please select the language of the SRT file')
      return
    }

    setIsUploading(true)
    setError('')

    try {
      const formData = new FormData()
      formData.append('file', selectedFile)
      const mode = burnFromWordTimings ? 'burn_words' : burnSubtitles ? 'burn' : 'generate'
      formData.append('mode', mode)
      if (mode === 'burn' && selectedSrtFile) {
        formData.append('srt_file', selectedSrtFile)
      }
      if (mode === 'burn_words' && selectedWordTimingsFile) {
        formData.append('word_timings_file', selectedWordTimingsFile)
      }

      const response = await fetch('http://localhost:8000/upload', {
        method: 'POST',
        body: formData,
      })

      if (!response.ok) {
        const errorData = await response.json().catch(() => null)
        throw new Error(errorData?.detail || 'Upload failed')
      }

      const data = await response.json()
      setJobId(data.job_id)
      setFileInfo({
        filename: data.filename,
        size: data.size,
        mode: data.mode,
        srt_filename: data.srt_filename,
        word_timings_filename: data.word_timings_filename,
        word_count: data.word_count,
      })
            
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setIsUploading(false)
    }
  }, [selectedFile, selectedSrtFile, selectedWordTimingsFile, burnSubtitles, burnFromWordTimings, sourceLanguage])

  const startProcessing = useCallback(async (jobId: string) => {
    setIsProcessing(true)
    
    try {
      const formData = new FormData()
      const mode = fileInfo?.mode
      const isBurnWords = mode === 'burn_words' || burnFromWordTimings
      const isBurnMode = mode === 'burn' || (burnSubtitles && !isBurnWords)

      if (!isBurnWords) {
        formData.append('target_language', targetLanguage)
        formData.append('karaoke', karaokeEnabled ? 'true' : 'false')
        if (sourceLanguage) {
          formData.append('source_language', sourceLanguage)
        } else if (isBurnMode) {
          throw new Error('Please select the language of the SRT file')
        }
      }

      const response = await fetch(`http://localhost:8000/process/${jobId}`, {
        method: 'POST',
        body: formData,
      })

      if (!response.ok) {
        const errorData = await response.json().catch(() => null)
        throw new Error(errorData?.detail || 'Failed to start processing')
      }

      pollStatus(jobId)
      
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Processing failed')
      setIsProcessing(false)
    }
  }, [targetLanguage, sourceLanguage, fileInfo, burnSubtitles, burnFromWordTimings, karaokeEnabled])

  const fetchWordTimings = useCallback(async (id: string) => {
    try {
      const response = await fetch(`http://localhost:8000/word-timings/${id}`)
      if (!response.ok) {
        setWordTimings([])
        return
      }
      const data = await response.json()
      setWordTimings(data.words || [])
      if (data.window_size) {
        setKaraokeWindowSize(data.window_size)
      }
      if (data.layout) {
        setOverlayLayout({
          width_pct: data.layout.width_pct ?? 80,
          left_pct: data.layout.left_pct ?? 10,
          bottom_pct: data.layout.bottom_pct ?? 5,
          height_pct: data.layout.height_pct ?? 15,
        })
      }
    } catch (err) {
      console.error('Failed to fetch word timings:', err)
      setWordTimings([])
    }
  }, [])

  const pollStatus = useCallback(async (jobId: string) => {
    const poll = async () => {
      try {
        const response = await fetch(`http://localhost:8000/status/${jobId}`)
        if (response.ok) {
          const status = await response.json()
          setJobStatus(status)
          
          if (status.status === 'completed') {
            setIsProcessing(false)
            if (status.has_word_timings || status.karaoke) {
              await fetchWordTimings(jobId)
            }
          } else if (status.status === 'failed') {
            setError(status.message)
            setIsProcessing(false)
          } else if (status.status === 'transcription_complete') {
            // transcription is ready for review
            setIsProcessing(false)
            await fetchTranscription(jobId)
          } else {
            setTimeout(poll, 1000)
          }
        }
      } catch (err) {
        console.error('Polling error:', err)
        setTimeout(poll, 2000)
      }
    }
    
    poll()
  }, [fetchWordTimings])

  const fetchTranscription = useCallback(async (jobId: string) => {
    try {
      const response = await fetch(`http://localhost:8000/transcription/${jobId}`)
      if (response.ok) {
        const data = await response.json()
        setTranscription(data.transcription)
        setIsEditingTranscription(true)
      }
    } catch (err) {
      setError('Failed to fetch transcription')
    }
  }, [])

  const continueWithTranscription = useCallback(async (editedTranscription: any) => {
    try {
      setIsEditingTranscription(false)
      setIsProcessing(true)
      
      const response = await fetch(`http://localhost:8000/transcription/${jobId}/continue`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(editedTranscription)
      })

      if (response.ok) {
        pollStatus(jobId)
      } else {
        setError('Failed to continue processing')
        setIsProcessing(false)
      }
    } catch (err) {
      setError('Failed to continue processing')
      setIsProcessing(false)
    }
  }, [jobId, pollStatus])

  const updateTranscriptionSegment = useCallback((index: number, newText: string) => {
    if (transcription && transcription.segments) {
      const updatedSegments = [...transcription.segments]
      updatedSegments[index].text = newText
      
      const updatedText = updatedSegments.map(seg => seg.text).join(' ')
      
      setTranscription({
        ...transcription,
        segments: updatedSegments,
        text: updatedText
      })
    }
  }, [transcription])

  const secondsToSrtTime = useCallback((seconds: number): string => {
    const hours = Math.floor(seconds / 3600)
    const minutes = Math.floor((seconds % 3600) / 60)
    const secs = Math.floor(seconds % 60)
    const milliseconds = Math.round((seconds - Math.floor(seconds)) * 1000)

    return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')},${milliseconds.toString().padStart(3, '0')}`
  }, [])

  const generateSrtContent = useCallback((segments: Array<{ start: number; end: number; text: string }>): string => {
    const lines: string[] = []
    let index = 1

    for (const segment of segments) {
      const text = segment.text?.trim()
      if (!text) continue

      lines.push(String(index))
      lines.push(`${secondsToSrtTime(segment.start)} --> ${secondsToSrtTime(segment.end)}`)
      lines.push(text)
      lines.push('')
      index += 1
    }

    return lines.join('\n')
  }, [secondsToSrtTime])

  const downloadSrt = useCallback(() => {
    if (!transcription?.segments?.length) return

    try {
      const srtContent = generateSrtContent(transcription.segments)
      const blob = new Blob([srtContent], { type: 'application/x-subrip;charset=utf-8' })
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `subtitles_${jobId || 'export'}.srt`
      a.click()
      window.URL.revokeObjectURL(url)
    } catch (err) {
      setError('SRT export failed')
    }
  }, [transcription, generateSrtContent, jobId])

  const downloadVideo = useCallback(async () => {
    if (!jobId) return
    
    try {
      const response = await fetch(`http://localhost:8000/download/${jobId}`)
      if (response.ok) {
        const blob = await response.blob()
        const url = window.URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `subtitled_video_${jobId}.mp4`
        a.click()
        window.URL.revokeObjectURL(url)
      }
    } catch (err) {
      setError('Download failed')
    }
  }, [jobId])

  const downloadWordTimingsFile = useCallback(async () => {
    if (!jobId) return
    try {
      const response = await fetch(`http://localhost:8000/download/word-timings/${jobId}`)
      if (!response.ok) {
        const errorData = await response.json().catch(() => null)
        throw new Error(errorData?.detail || 'Word timings download failed')
      }
      const blob = await response.blob()
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `word_timings_${jobId}.json`
      a.click()
      window.URL.revokeObjectURL(url)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Word timings download failed')
    }
  }, [jobId])

  const resetForm = useCallback(() => {
    setSelectedFile(null)
    setSelectedSrtFile(null)
    setSelectedWordTimingsFile(null)
    setBurnSubtitles(false)
    setBurnFromWordTimings(false)
    setKaraokeEnabled(true)
    setJobId('')
    setJobStatus(null)
    setFileInfo(null)
    setIsUploading(false)
    setIsProcessing(false)
    setError('')
    setShowVideoPreview(false)
    setTranscription(null)
    setIsEditingTranscription(false)
    setWordTimings([])
    setActiveWordIndex(-1)
    if (fileInputRef.current) {
      fileInputRef.current.value = ''
    }
    if (srtInputRef.current) {
      srtInputRef.current.value = ''
    }
    if (wordTimingsInputRef.current) {
      wordTimingsInputRef.current.value = ''
    }
  }, [])

  const getKaraokeWindow = useCallback((activeIndex: number) => {
    if (activeIndex < 0 || wordTimings.length === 0) return []
    const window = Math.max(1, karaokeWindowSize)
    const half = Math.floor(window / 2)
    let start = Math.max(0, activeIndex - half)
    let end = Math.min(wordTimings.length, start + window)
    start = Math.max(0, end - window)
    return wordTimings.slice(start, end).map((w, offset) => ({
      ...w,
      index: start + offset,
      is_active: start + offset === activeIndex,
    }))
  }, [wordTimings, karaokeWindowSize])

  const handlePreviewTimeUpdate = useCallback(() => {
    const video = previewVideoRef.current
    if (!video || wordTimings.length === 0) {
      setActiveWordIndex(-1)
      return
    }
    const t = video.currentTime
    let idx = -1
    for (let i = 0; i < wordTimings.length; i++) {
      if (t >= wordTimings[i].start && t < wordTimings[i].end) {
        idx = i
        break
      }
      if (t >= wordTimings[i].start) {
        idx = i
      }
    }
    setActiveWordIndex(idx)
  }, [wordTimings])

  useEffect(() => {
    if (jobStatus?.status === 'completed' && wordTimings.length === 0 && jobId && (jobStatus.karaoke || jobStatus.has_word_timings)) {
      fetchWordTimings(jobId)
    }
  }, [jobStatus, wordTimings.length, jobId, fetchWordTimings])

  return (
    <div className="min-h-screen bg-white">
      <div className="max-w-7xl mx-auto px-4 py-8">
        <div className="text-center mb-12">
          <div className="flex justify-center mb-8">
          <img 
                src="/groq-labs-logo.png" 
                alt="GroqLabs Logo" 
                className="h-15 w-auto"
              />
          </div>
        </div>
        </div>

      <div className="max-w-7xl mx-auto px-6">
        {/* title section */}
        <div className="text-center mb-12">
          <h1 className="text-5xl font-bold text-gray-900 mb-4">Auto Multilingual Subtitle Generator</h1>
          <p className="text-xl text-gray-600 mb-2 py-2">
          Lightning-fast AI-powered multilingual subtitles.
          </p>
          <p className="text-xl text-gray-600 mb-2 py-2">
          Powered by <span className="font-semibold">Groq.</span>
          </p>
          </div>

        {/* main area */}
        <div className="max-w-4xl mx-auto">
          <div className="bg-white border border-gray-200 rounded-lg p-8 shadow-sm">
            {!jobId ? (
              /* upload section */
              <div className="space-y-8">
                {/* file upload */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-3">Select Video File</label>
                  <div
                    className={`border-2 border-dashed rounded-lg p-12 text-center transition-all duration-300 ${
                      dragActive
                        ? "border-orange-500 bg-orange-50"
                        : "border-gray-300 hover:border-orange-400 hover:bg-gray-50"
                    }`}
                    onDragEnter={handleDrag}
                    onDragLeave={handleDrag}
                    onDragOver={handleDrag}
                    onDrop={handleDrop}
                  >
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept="video/*"
                      onChange={handleFileSelect}
                      className="hidden"
                    />
                    {selectedFile ? (
                      <div className="space-y-4">
                        <div className="flex items-center justify-center space-x-3">
                          <FileText className="w-12 h-12 text-orange-500" />
                          <div className="text-left min-w-0 flex-1">
                            <span className="text-xl font-medium text-gray-900 block truncate max-w-xs" title={selectedFile.name}>
                              {selectedFile.name}
                            </span>
                            <p className="text-gray-500">{formatFileSize(selectedFile.size)}</p>
                          </div>
                        </div>
                      </div>
                    ) : (
                      <div className="space-y-4">
                        <Upload className="w-16 h-16 text-gray-400 mx-auto" />
                        <div>
                          <p className="text-xl font-medium text-gray-900 mb-2">
                            Drop your video file here or click to browse
                          </p>
                          <p className="text-gray-500">Supports MP4, MOV or AVI up to 25MB</p>
                          <p className="text-gray-500 text-sm mt-1">Keep videos under 5-10 minutes for optimal performance</p>
                        </div>
                      </div>
                    )}
                    <button
                      onClick={() => fileInputRef.current?.click()}
                      className="mt-6 px-8 py-3 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors font-medium border border-gray-300"
                    >
                      Choose File
                    </button>
                  </div>
                </div>

                {/* mode selection */}
                <div className="bg-gray-50 rounded-lg p-5 border border-gray-200 space-y-4">
                  <label className="flex items-start space-x-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={burnSubtitles && !burnFromWordTimings}
                      onChange={(e) => {
                        const on = e.target.checked
                        setBurnSubtitles(on)
                        if (on) {
                          setBurnFromWordTimings(false)
                          setSelectedWordTimingsFile(null)
                          if (wordTimingsInputRef.current) wordTimingsInputRef.current.value = ''
                        } else {
                          setSelectedSrtFile(null)
                          if (srtInputRef.current) srtInputRef.current.value = ''
                        }
                        setError('')
                      }}
                      className="mt-1 h-4 w-4 text-orange-500 border-gray-300 rounded focus:ring-orange-500"
                    />
                    <span>
                      <span className="block text-sm font-medium text-gray-900">Burn Video Subtitles (SRT)</span>
                      <span className="block text-sm text-gray-500 mt-1">
                        Upload an existing SRT and burn it into the video. Skips transcription; optional translation + karaoke align.
                      </span>
                    </span>
                  </label>
                  <label className="flex items-start space-x-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={burnFromWordTimings}
                      onChange={(e) => {
                        const on = e.target.checked
                        setBurnFromWordTimings(on)
                        if (on) {
                          setBurnSubtitles(true)
                          setKaraokeEnabled(true)
                          setSelectedSrtFile(null)
                          if (srtInputRef.current) srtInputRef.current.value = ''
                        } else {
                          setSelectedWordTimingsFile(null)
                          if (wordTimingsInputRef.current) wordTimingsInputRef.current.value = ''
                          setBurnSubtitles(false)
                        }
                        setError('')
                      }}
                      className="mt-1 h-4 w-4 text-orange-500 border-gray-300 rounded focus:ring-orange-500"
                    />
                    <span>
                      <span className="block text-sm font-medium text-gray-900">Burn from Word Timings</span>
                      <span className="block text-sm text-gray-500 mt-1">
                        Upload a word_timings.json with the video to skip alignment and only burn karaoke subtitles.
                      </span>
                    </span>
                  </label>
                  <label className={`flex items-start space-x-3 cursor-pointer ${burnFromWordTimings ? 'opacity-50' : ''}`}>
                    <input
                      type="checkbox"
                      checked={karaokeEnabled}
                      disabled={burnFromWordTimings}
                      onChange={(e) => setKaraokeEnabled(e.target.checked)}
                      className="mt-1 h-4 w-4 text-orange-500 border-gray-300 rounded focus:ring-orange-500"
                    />
                    <span>
                      <span className="block text-sm font-medium text-gray-900">Karaoke Subtitles</span>
                      <span className="block text-sm text-gray-500 mt-1">
                        Word-level highlighting with a running text window (WhisperX forced alignment). Always on for word-timings burn.
                      </span>
                    </span>
                  </label>
                </div>

                {/* SRT upload for burn mode */}
                {burnSubtitles && !burnFromWordTimings && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-3">SRT Subtitle File *</label>
                    <div className="border border-gray-300 rounded-lg p-4 bg-white">
                      <input
                        ref={srtInputRef}
                        type="file"
                        accept=".srt,application/x-subrip,text/plain"
                        onChange={handleSrtSelect}
                        className="hidden"
                      />
                      {selectedSrtFile ? (
                        <div className="flex items-center justify-between gap-4">
                          <div className="flex items-center space-x-3 min-w-0">
                            <FileText className="w-8 h-8 text-orange-500 flex-shrink-0" />
                            <div className="min-w-0">
                              <span className="text-sm font-medium text-gray-900 block truncate" title={selectedSrtFile.name}>
                                {selectedSrtFile.name}
                              </span>
                              <p className="text-gray-500 text-sm">{formatFileSize(selectedSrtFile.size)}</p>
                            </div>
                          </div>
                          <button
                            type="button"
                            onClick={() => srtInputRef.current?.click()}
                            className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors text-sm font-medium border border-gray-300"
                          >
                            Change
                          </button>
                        </div>
                      ) : (
                        <div className="text-center space-y-3">
                          <p className="text-sm text-gray-600">Select an .srt file to burn into your video</p>
                          <button
                            type="button"
                            onClick={() => srtInputRef.current?.click()}
                            className="px-6 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors font-medium border border-gray-300"
                          >
                            Choose SRT File
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {/* Word timings upload */}
                {burnFromWordTimings && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-3">Word Timings JSON *</label>
                    <div className="border border-gray-300 rounded-lg p-4 bg-white">
                      <input
                        ref={wordTimingsInputRef}
                        type="file"
                        accept=".json,application/json"
                        onChange={handleWordTimingsSelect}
                        className="hidden"
                      />
                      {selectedWordTimingsFile ? (
                        <div className="flex items-center justify-between gap-4">
                          <div className="flex items-center space-x-3 min-w-0">
                            <FileText className="w-8 h-8 text-orange-500 flex-shrink-0" />
                            <div className="min-w-0">
                              <span className="text-sm font-medium text-gray-900 block truncate" title={selectedWordTimingsFile.name}>
                                {selectedWordTimingsFile.name}
                              </span>
                              <p className="text-gray-500 text-sm">{formatFileSize(selectedWordTimingsFile.size)}</p>
                            </div>
                          </div>
                          <button
                            type="button"
                            onClick={() => wordTimingsInputRef.current?.click()}
                            className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors text-sm font-medium border border-gray-300"
                          >
                            Change
                          </button>
                        </div>
                      ) : (
                        <div className="text-center space-y-3">
                          <p className="text-sm text-gray-600">Select a word_timings.json file (skips alignment)</p>
                          <button
                            type="button"
                            onClick={() => wordTimingsInputRef.current?.click()}
                            className="px-6 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors font-medium border border-gray-300"
                          >
                            Choose JSON File
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {/* language selection */}
                {!burnFromWordTimings && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-3">
                      {burnSubtitles ? 'SRT Language *' : 'Source Language'}
                    </label>
                    <select
                      value={sourceLanguage}
                      onChange={(e) => setSourceLanguage(e.target.value)}
                      className="w-full px-4 py-3 bg-white border border-gray-300 rounded-lg text-gray-900 focus:outline-none focus:ring-2 focus:ring-orange-500 focus:border-transparent"
                    >
                      {!burnSubtitles && <option value="">Auto-detect</option>}
                      {burnSubtitles && <option value="" disabled>Select SRT language</option>}
                      {Object.entries(SUPPORTED_LANGUAGES).map(([code, name]) => (
                        <option key={code} value={code}>
                          {name}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-3">Target Language *</label>
                    <select
                      value={targetLanguage}
                      onChange={(e) => setTargetLanguage(e.target.value)}
                      className="w-full px-4 py-3 bg-white border border-gray-300 rounded-lg text-gray-900 focus:outline-none focus:ring-2 focus:ring-orange-500 focus:border-transparent"
                    >
                      {Object.entries(SUPPORTED_LANGUAGES).map(([code, name]) => (
                        <option key={code} value={code}>
                          {name}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
                )}

                <button
                  onClick={uploadVideo}
                  disabled={
                    !selectedFile
                    || isUploading
                    || (burnFromWordTimings && !selectedWordTimingsFile)
                    || (burnSubtitles && !burnFromWordTimings && (!selectedSrtFile || !sourceLanguage))
                  }
                  className="w-full flex items-center justify-center space-x-3 px-8 py-4 bg-orange-500 text-white rounded-lg font-semibold hover:bg-orange-600 disabled:opacity-50 disabled:cursor-not-allowed transition-all text-lg"
                >
                  {isUploading ? (
                    <>
                      <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-white"></div>
                      <span>Uploading...</span>
                    </>
                  ) : (
                    <>
                      <Upload className="w-6 h-6" />
                      <span>Upload Video</span>
                    </>
                  )}
                </button>
              </div>
            ) : !jobStatus ? (
              /* ready to process section */
              <div className="space-y-8">
                {/* file info - using reusable component */}
                {fileInfo && (
                  <FileInfoSection
                    fileInfo={fileInfo}
                    title="File Uploaded Successfully"
                    icon={FileText}
                    gradientFrom="blue-50"
                    gradientTo="indigo-50"
                    borderColor="blue-200"
                    iconColor="blue"
                    formatFileSize={formatFileSize}
                    formatDuration={formatDuration}
                    getFileExtension={getFileExtension}
                    getAspectRatio={getAspectRatio}
                    getEstimatedBitrate={getEstimatedBitrate}
                  />
                )}

                <div className="bg-gray-50 rounded-lg p-6 border border-gray-200">
                  <h3 className="font-semibold text-gray-900 mb-4 text-lg">
                    {fileInfo?.mode === 'burn_words'
                      ? 'Burn from Word Timings'
                      : fileInfo?.mode === 'burn'
                      ? 'Burn Settings'
                      : 'Language Settings'}
                  </h3>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {fileInfo?.mode === 'burn' && (
                      <div className="md:col-span-2">
                        <span className="text-gray-500 block text-sm">SRT File</span>
                        <span className="text-gray-900 font-medium block truncate" title={fileInfo.srt_filename || ''}>
                          {fileInfo.srt_filename || 'Uploaded'}
                        </span>
                      </div>
                    )}
                    {fileInfo?.mode === 'burn_words' && (
                      <>
                        <div className="md:col-span-2">
                          <span className="text-gray-500 block text-sm">Word Timings File</span>
                          <span className="text-gray-900 font-medium block truncate" title={fileInfo.word_timings_filename || ''}>
                            {fileInfo.word_timings_filename || 'Uploaded'}
                          </span>
                        </div>
                        <div>
                          <span className="text-gray-500 block text-sm">Words</span>
                          <span className="text-gray-900 font-medium">{fileInfo.word_count ?? '—'}</span>
                        </div>
                      </>
                    )}
                    {fileInfo?.mode !== 'burn_words' && (
                      <>
                        <div>
                          <span className="text-gray-500 block text-sm">
                            {fileInfo?.mode === 'burn' ? 'SRT Language' : 'Source Language'}
                          </span>
                          <span className="text-gray-900 font-medium">
                            {sourceLanguage ? SUPPORTED_LANGUAGES[sourceLanguage as keyof typeof SUPPORTED_LANGUAGES] : 'Auto-detect'}
                          </span>
                        </div>
                        <div>
                          <span className="text-gray-500 block text-sm">Target Language</span>
                          <span className="text-gray-900 font-medium">
                            {SUPPORTED_LANGUAGES[targetLanguage as keyof typeof SUPPORTED_LANGUAGES]}
                          </span>
                        </div>
                      </>
                    )}
                    <div className="md:col-span-2">
                      <span className="text-gray-500 block text-sm">Karaoke Subtitles</span>
                      <span className="text-gray-900 font-medium">
                        {fileInfo?.mode === 'burn_words' || karaokeEnabled
                          ? 'Enabled (word-level highlight)'
                          : 'Disabled'}
                      </span>
                    </div>
                  </div>
                </div>

                {/* process button */}
                <button
                  onClick={() => startProcessing(jobId)}
                  disabled={isProcessing}
                  className="w-full flex items-center justify-center space-x-3 px-8 py-4 bg-orange-500 text-white rounded-lg font-semibold hover:bg-orange-600 disabled:opacity-50 disabled:cursor-not-allowed transition-all text-lg"
                >
                  {isProcessing ? (
                    <>
                      <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-white"></div>
                      <span>Starting Processing...</span>
                    </>
                  ) : (
                    <>
                      <Play className="w-6 h-6" />
                      <span>
                        {fileInfo?.mode === 'burn_words'
                          ? 'Burn Karaoke into Video'
                          : fileInfo?.mode === 'burn'
                          ? 'Burn Subtitles into Video'
                          : 'Start Processing'}
                      </span>
                    </>
                  )}
                </button>

                {/* back button */}
                <button
                  onClick={resetForm}
                  className="w-full px-8 py-3 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors font-medium border border-gray-300"
                >
                  Upload Different Video
                </button>
              </div>
            ) : (
              /* processing section */
              <div className="space-y-8">
                {/* file info - using same reusable component with different styling */}
                {fileInfo && (
                  <FileInfoSection
                    fileInfo={fileInfo}
                    title="File Information"
                    icon={CheckCircle}
                    gradientFrom="green-50"
                    gradientTo="emerald-50"
                    borderColor="green-200"
                    iconColor="green"
                    formatFileSize={formatFileSize}
                    formatDuration={formatDuration}
                    getFileExtension={getFileExtension}
                    getAspectRatio={getAspectRatio}
                    getEstimatedBitrate={getEstimatedBitrate}
                  />
                )}

                {/* progress */}
                <div className="space-y-4">
                  <div className="flex items-center justify-between">
                    <h3 className="font-semibold text-gray-900 text-lg">Processing Progress</h3>
                    <span className="text-orange-500 font-bold text-lg">{jobStatus.progress}%</span>
                  </div>
                  <div className="w-full bg-gray-200 rounded-full h-3">
                    <div
                      className="bg-orange-500 h-3 rounded-full transition-all duration-500"
                      style={{ width: `${jobStatus.progress}%` }}
                    ></div>
                  </div>
                  <div className="flex items-center space-x-3">
                    {jobStatus.status === "completed" ? (
                      <CheckCircle className="w-6 h-6 text-green-500" />
                    ) : jobStatus.status === "failed" ? (
                      <AlertCircle className="w-6 h-6 text-red-500" />
                    ) : jobStatus.status === "transcription_complete" ? (
                      <FileText className="w-6 h-6 text-blue-500" />
                    ) : (
                      <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-orange-500"></div>
                    )}
                    <span className="text-gray-600">
                      {jobStatus.status === "transcription_complete" 
                        ? "Transcription complete - Please review and edit if needed"
                        : jobStatus.status === "aligning"
                        ? "Aligning words to audio (WhisperX) — this can take a few minutes on CPU..."
                        : jobStatus.status === "generating_karaoke"
                        ? "Building karaoke subtitles..."
                        : jobStatus.status === "translating" && fileInfo?.mode === "burn"
                        ? "Translating SRT subtitles..."
                        : jobStatus.status === "rendering_video"
                        ? (karaokeEnabled || jobStatus.karaoke
                          ? "Burning karaoke subtitles into video..."
                          : "Burning subtitles into video...")
                        : (jobStatus.message || jobStatus.status)
                      }
                    </span>
                  </div>
                </div>

                {/* download buttons */}
                {jobStatus.status === "completed" && (
                  <div className="space-y-3">
                    <button
                      onClick={downloadVideo}
                      className="w-full flex items-center justify-center space-x-3 px-8 py-4 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors font-semibold text-lg"
                    >
                      <Download className="w-6 h-6" />
                      <span>Download Video with Subtitles</span>
                    </button>
                    {wordTimings.length > 0 && (
                      <button
                        onClick={downloadWordTimingsFile}
                        className="w-full flex items-center justify-center space-x-3 px-8 py-3 border border-orange-500 text-orange-600 rounded-lg hover:bg-orange-50 transition-colors font-medium"
                      >
                        <Download className="w-5 h-5" />
                        <span>Download Word Timings (JSON)</span>
                      </button>
                    )}
                  </div>
                )}

                {/* reset button */}
                <button
                  onClick={resetForm}
                  className="w-full px-6 py-3 border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 transition-colors font-medium"
                >
                  Process Another Video
                </button>
              </div>
            )}

            {/* error message */}
            {error && (
              <div className="mt-6 p-4 bg-red-50 border border-red-200 rounded-lg">
                <div className="flex items-center space-x-2">
                  <AlertCircle className="w-5 h-5 text-red-500" />
                  <span className="text-red-700">{error}</span>
                </div>
              </div>
            )}

            {/* transcription editing */}
            {isEditingTranscription && transcription && (
              <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                <div className="bg-white rounded-lg max-w-4xl w-full max-h-[90vh] overflow-hidden">
                  <div className="p-6 border-b border-gray-200">
                    <h3 className="text-xl font-bold text-gray-900 mb-2">Review and Edit Transcription</h3>
                    <p className="text-gray-600">
                      Please review the transcription below and make any necessary corrections before continuing.
                    </p>
                  </div>
                  
                  <div className="p-6 overflow-y-auto max-h-[60vh]">
                    <div className="space-y-4">
                      {transcription.segments.map((segment: any, index: number) => (
                        <div key={index} className="border border-gray-200 rounded-lg p-4">
                          <div className="flex items-center justify-between mb-2">
                            <span className="text-sm font-medium text-gray-500">
                              {Math.floor(segment.start / 60)}:{(segment.start % 60).toFixed(1).padStart(4, '0')} - {Math.floor(segment.end / 60)}:{(segment.end % 60).toFixed(1).padStart(4, '0')}
                            </span>
                          </div>
                          <textarea
                            value={segment.text}
                            onChange={(e) => updateTranscriptionSegment(index, e.target.value)}
                            className="w-full p-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent resize-none"
                            rows={2}
                            placeholder="Edit transcription text..."
                          />
                        </div>
                      ))}
                    </div>
                  </div>
                  
                  <div className="p-6 border-t border-gray-200 bg-gray-50">
                    <div className="flex items-center justify-between gap-4 flex-wrap">
                      <div className="text-sm text-gray-600">
                        <p><strong>Detected Language:</strong> {transcription.detected_language}</p>
                      </div>
                      <div className="flex items-center space-x-3 flex-wrap justify-end">
                        <button
                          onClick={() => {
                            setIsEditingTranscription(false)
                            setTranscription(null)
                          }}
                          className="px-6 py-2 border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 transition-colors"
                        >
                          Cancel
                        </button>
                        <button
                          onClick={downloadSrt}
                          className="flex items-center space-x-2 px-6 py-2 border border-orange-500 text-orange-600 rounded-lg hover:bg-orange-50 transition-colors font-medium"
                        >
                          <Download className="w-4 h-4" />
                          <span>Export SRT</span>
                        </button>
                        <button
                          onClick={() => continueWithTranscription(transcription)}
                          className="px-6 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 transition-colors font-medium"
                        >
                          Continue Processing
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* video preview section */}
        {jobStatus?.status === "completed" && (
          <div className="max-w-6xl mx-auto mt-8">
            <div className="bg-white border border-gray-200 rounded-lg p-8 shadow-sm">
              <div className="flex items-center justify-between mb-6">
                <h3 className="text-2xl font-bold text-gray-900">Video Preview</h3>
                <button
                  onClick={() => setShowVideoPreview(!showVideoPreview)}
                  className="flex items-center space-x-2 px-6 py-3 bg-orange-500 text-white rounded-lg hover:bg-orange-600 transition-colors font-medium"
                >
                  <Eye className="w-5 h-5" />
                  <span>{showVideoPreview ? "Hide Preview" : "Show Preview"}</span>
                </button>
              </div>
              {showVideoPreview && (
                <div className="flex justify-center">
                  <div className="w-full max-w-4xl space-y-4">
                    <h4 className="font-semibold text-gray-700 flex items-center justify-center space-x-2 text-lg">
                      <FileText className="w-5 h-5" />
                      <span>Video with Subtitles</span>
                    </h4>
                    <div className="bg-black rounded-lg overflow-hidden relative">
                      <video
                        ref={previewVideoRef}
                        controls
                        className="w-full h-auto max-h-96 object-contain"
                        src={`http://localhost:8000/video/preview/${jobId}`}
                        preload="metadata"
                        onTimeUpdate={handlePreviewTimeUpdate}
                        onSeeked={handlePreviewTimeUpdate}
                        onPlay={handlePreviewTimeUpdate}
                      >
                        Your browser does not support the video tag.
                      </video>
                      {wordTimings.length > 0 && (
                        <div
                          className="absolute pointer-events-none flex items-center justify-center"
                          style={{
                            left: `${overlayLayout.left_pct}%`,
                            width: `${overlayLayout.width_pct}%`,
                            bottom: `${overlayLayout.bottom_pct}%`,
                            height: `${overlayLayout.height_pct}%`,
                          }}
                        >
                          <div className="w-full bg-black/70 rounded-lg px-4 py-2 flex items-center justify-center max-h-full overflow-hidden">
                            <p className="text-center text-base md:text-xl leading-relaxed flex flex-wrap justify-center gap-x-2 gap-y-1">
                              {getKaraokeWindow(activeWordIndex).map((w) => (
                                <span
                                  key={`${w.index}-${w.word}`}
                                  className={
                                    w.is_active
                                      ? 'text-orange-400 font-bold'
                                      : 'text-white font-medium'
                                  }
                                >
                                  {w.word}
                                </span>
                              ))}
                            </p>
                          </div>
                        </div>
                      )}
                    </div>
                    {wordTimings.length > 0 && (
                      <p className="text-center text-sm text-gray-500">
                        Live karaoke overlay ({wordTimings.length} words aligned)
                      </p>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

      </div>
    </div>
  )
  
}
