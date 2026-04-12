package com.terevos.pdfreader

import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.view.View
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.tabs.TabLayoutMediator
import com.terevos.pdfreader.databinding.ActivityMainBinding
import com.terevos.pdfreader.util.FileUtils

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var tabAdapter: TabAdapter
    private val tabs = mutableListOf<PdfTab>()

    private val openPdfLauncher =
        registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
            uri ?: return@registerForActivityResult
            val filename = FileUtils.getFilename(this, uri)
            val tab = PdfTab(uri = uri, filename = filename)
            tabAdapter.addTab(tab)
            binding.viewPager.currentItem = tabs.size - 1
            updateEmptyState()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setSupportActionBar(binding.toolbar)

        tabAdapter = TabAdapter(supportFragmentManager, lifecycle, tabs)
        binding.viewPager.adapter = tabAdapter
        binding.viewPager.offscreenPageLimit = 2

        TabLayoutMediator(binding.tabLayout, binding.viewPager) { tab, position ->
            tab.text = tabs.getOrNull(position)?.filename ?: "PDF"
            // Long-press on a tab closes it
            tab.view.setOnLongClickListener {
                confirmCloseTab(position)
                true
            }
        }.attach()

        binding.fabOpen.setOnClickListener {
            openPdfLauncher.launch("application/pdf")
        }

        // Support opening a PDF via intent (e.g. from a file manager)
        intent?.data?.let { uri ->
            val filename = FileUtils.getFilename(this, uri)
            tabAdapter.addTab(PdfTab(uri = uri, filename = filename))
            updateEmptyState()
        }

        updateEmptyState()
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.menu_main, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean = when (item.itemId) {
        R.id.action_close_all -> {
            if (tabs.isNotEmpty()) {
                AlertDialog.Builder(this)
                    .setTitle(R.string.close_all)
                    .setMessage("Close all open PDFs?")
                    .setPositiveButton(R.string.close_all) { _, _ ->
                        val count = tabs.size
                        tabs.clear()
                        tabAdapter.notifyItemRangeRemoved(0, count)
                        updateEmptyState()
                    }
                    .setNegativeButton(android.R.string.cancel, null)
                    .show()
            }
            true
        }
        R.id.action_about -> {
            AlertDialog.Builder(this)
                .setTitle(R.string.app_name)
                .setMessage("Open-source PDF reader for Android.\nLicensed under GNU GPL v3.\n\nhttps://github.com/dan-roca/openpdf-reader")
                .setPositiveButton(android.R.string.ok, null)
                .show()
            true
        }
        else -> super.onOptionsItemSelected(item)
    }

    private fun confirmCloseTab(position: Int) {
        AlertDialog.Builder(this)
            .setMessage("Close \"${tabs.getOrNull(position)?.filename}\"?")
            .setPositiveButton("Close") { _, _ ->
                tabAdapter.removeTab(position)
                updateEmptyState()
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    private fun updateEmptyState() {
        binding.emptyState.visibility = if (tabs.isEmpty()) View.VISIBLE else View.GONE
        binding.viewPager.visibility  = if (tabs.isEmpty()) View.GONE   else View.VISIBLE
    }
}
