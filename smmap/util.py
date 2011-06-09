"""Module containnig a memory memory manager which provides a sliding window on a number of memory mapped files"""
import os
import sys
import mmap

from mmap import PAGESIZE, mmap, ACCESS_READ
from sys import getrefcount

__all__ = [	"align_to_page", "is_64_bit",
			"MemoryWindow", "MappedRegion", "MappedRegionList", "PAGESIZE"]

#{ Utilities

def align_to_page(num, round_up):
	"""Align the given integer number to the closest page offset, which usually is 4096 bytes.
	:param round_up: if True, the next higher multiple of page size is used, otherwise
		the lower page_size will be used (i.e. if True, 1 becomes 4096, otherwise it becomes 0)
	:return: num rounded to closest page"""
	res = (num / PAGESIZE) * PAGESIZE;
	if round_up and (res != num):
		res += PAGESIZE;
	#END handle size
	return res;
	
def is_64_bit():
	""":return: True if the system is 64 bit. Otherwise it can be assumed to be 32 bit"""
	return sys.maxint > (1<<32) - 1

#}END utilities


#{ Utility Classes 

class MemoryWindow(object):
	"""Utility type which is used to snap windows towards each other, and to adjust their size"""
	__slots__ = (
				'ofs',		# offset into the file in bytes
				'size'				# size of the window in bytes
				)

	def __init__(self, offset, size):
		self.ofs = offset
		self.size = size

	def __repr__(self):
		return "MemoryWindow(%i, %i)" % (self.ofs, self.size) 

	@classmethod
	def from_region(cls, region):
		""":return: new window from a region"""
		return cls(region._b, region.size())

	def ofs_end(self):
		return self.ofs + self.size

	def align(self):
		"""Assures the previous window area is contained in the new one"""
		nofs = align_to_page(self.ofs, 0)
		self.size += self.ofs - nofs	# keep size constant
		self.ofs = nofs
		self.size = align_to_page(self.size, 1)

	def extend_left_to(self, window, max_size):
		"""Adjust the offset to start where the given window on our left ends if possible, 
		but don't make yourself larger than max_size.
		The resize will assure that the new window still contains the old window area"""
		rofs = self.ofs - window.ofs_end()
		nsize = rofs + self.size
		rofs -= nsize - min(nsize, max_size)
		self.ofs = self.ofs - rofs
		self.size += rofs

	def extend_right_to(self, window, max_size):
		"""Adjust the size to make our window end where the right window begins, but don't
		get larger than max_size"""
		self.size = min(self.size + (window.ofs - self.ofs_end()), max_size)


class MappedRegion(object):
	"""Defines a mapped region of memory, aligned to pagesizes
	:note: deallocates used region automatically on destruction"""
	__slots__ = [
					'_b'	, 	# beginning of mapping
					'_mf',	# mapped memory chunk (as returned by mmap)
					'_uc',	# total amount of usages
					'_size', # cached size of our memory map
					'__weakref__'
				]
	_need_compat_layer = sys.version_info[1] < 6
	
	if _need_compat_layer:
		__slots__.append('_mfb')		# mapped memory buffer to provide offset
	#END handle additional slot
		
	
	def __init__(self, path, ofs, size, flags = 0):
		"""Initialize a region, allocate the memory map
		:param path: path to the file to map
		:param ofs: **aligned** offset into the file to be mapped 
		:param size: if size is larger then the file on disk, the whole file will be
			allocated the the size automatically adjusted
		:param flags: additional flags to be given when opening the file. 
		:raise Exception: if no memory can be allocated"""
		self._b = ofs
		self._size = 0
		self._uc = 0
		
		fd = os.open(path, os.O_RDONLY|getattr(os, 'O_BINARY', 0)|flags)
		try:
			kwargs = dict(access=ACCESS_READ, offset=ofs)
			corrected_size = size
			sizeofs = ofs
			if self._need_compat_layer:
				del(kwargs['offset'])
				corrected_size += ofs
				sizeofs = 0
			# END handle python not supporting offset ! Arg
			
			# have to correct size, otherwise (instead of the c version) it will 
			# bark that the size is too large ... many extra file accesses because
			# if this ... argh !
			self._mf = mmap(fd, min(os.fstat(fd).st_size - sizeofs, corrected_size), **kwargs)
			self._size = len(self._mf)
			
			if self._need_compat_layer:
				self._mfb = buffer(self._mf, ofs, size)
			#END handle buffer wrapping
		finally:
			os.close(fd)
		#END close file handle
		
	def __repr__(self):
		return "MappedRegion<%i, %i>" % (self._b, self.size())
		
	#{ Interface
		
	def buffer(self):
		""":return: a sliceable buffer which can be used to access the mapped memory"""
		return self._mf
		
	def ofs_begin(self):
		""":return: absolute byte offset to the first byte of the mapping"""
		return self._b
		
	def size(self):
		""":return: total size of the mapped region in bytes"""
		return self._size
		
	def ofs_end(self):
		""":return: Absolute offset to one byte beyond the mapping into the file"""
		return self._b + self._size
		
	def includes_ofs(self, ofs):
		""":return: True if the given offset can be read in our mapped region"""
		return self._b <= ofs < self._b + self._size
		
	def client_count(self):
		""":return: number of clients currently using this region"""
		# -1: self on stack, -1 self in this method, -1 self in getrefcount
		return getrefcount(self)-3
		
	def usage_count(self):
		""":return: amount of usages so far"""
		return self._uc
		
	def increment_usage_count(self):
		"""Adjust the usage count by the given positive or negative offset"""
		self._uc += 1
		
	# re-define all methods which need offset adjustments in compatibility mode
	if _need_compat_layer:
		def size(self):
			return self._size - self._b
			
		def ofs_end(self):
			# always the size - we are as large as it gets
			return self._size
			
		def buffer(self):
			return self._mfb
			
		def includes_ofs(self, ofs):
			return self._b <= ofs < self._size
	#END handle compat layer
	
	#} END interface
	

class MappedRegionList(list):
	"""List of MappedRegion instances associating a path with a list of regions."""
	__slots__ = (
				'_path', 		# path which is mapped by all our regions
				'_file_size'		# total size of the file we map
				)
	
	def __new__(cls, path):
		return super(MappedRegionList, cls).__new__(cls)
	
	def __init__(self, path):
		self._path = path
		self._file_size = None
		
	def client_count(self):
		""":return: amount of clients which hold a reference to this instance"""
		return getrefcount(self)-3
		
	def path(self):
		""":return: path to file whose regions we manage"""
		return self._path
		
	def file_size(self):
		""":return: size of file we manager"""
		if self._file_size is None:
			self._file_size = os.stat(self._path).st_size
		#END update file size
		return self._file_size
	
#} END utilty classes