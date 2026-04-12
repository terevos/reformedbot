package com.terevos.pdfreader

import androidx.fragment.app.Fragment
import androidx.fragment.app.FragmentManager
import androidx.lifecycle.Lifecycle
import androidx.viewpager2.adapter.FragmentStateAdapter

class TabAdapter(
    fragmentManager: FragmentManager,
    lifecycle: Lifecycle,
    private val tabs: MutableList<PdfTab>
) : FragmentStateAdapter(fragmentManager, lifecycle) {

    override fun getItemCount(): Int = tabs.size

    override fun createFragment(position: Int): Fragment {
        val tab = tabs[position]
        return PdfViewerFragment.newInstance(tab.uri, tab.filename)
    }

    // Stable IDs prevent ViewPager2 from recreating fragments on tab list changes
    override fun getItemId(position: Int): Long = tabs[position].uri.toString().hashCode().toLong()
    override fun containsItem(itemId: Long): Boolean =
        tabs.any { it.uri.toString().hashCode().toLong() == itemId }

    fun addTab(tab: PdfTab) {
        tabs.add(tab)
        notifyItemInserted(tabs.size - 1)
    }

    fun removeTab(position: Int) {
        if (position in tabs.indices) {
            tabs.removeAt(position)
            notifyItemRemoved(position)
            notifyItemRangeChanged(position, tabs.size - position)
        }
    }
}
